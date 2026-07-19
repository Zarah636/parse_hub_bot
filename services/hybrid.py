import asyncio
from types import TracebackType

from easy_ai18n import PreLocaleSelector
from parsehub.types import AnyParseResult

from log import logger
from services.flyinglife import flyinglife
from services.pipeline import ParsePipeline, PipelineResult, StatusReporter

logger = logger.bind(name="HybridPipeline")

_inflight: dict[str, asyncio.Event] = {}


class HybridParsePipeline:
    """FlyingLife 优先、ParseHub 兜底的事务式解析流水线。"""

    def __init__(
        self,
        url: str,
        raw_url: str,
        reporter: StatusReporter,
        parse_result: AnyParseResult | None = None,
        *,
        platform_id: str,
        singleflight: bool = True,
        skip_media_processing: bool = False,
        skip_download_threshold: int = 0,
        gif_only_skip_download_count_threshold: int = 0,
        save_metadata: bool = False,
        flyinglife_parse_result: AnyParseResult | None = None,
        skip_flyinglife: bool = False,
        _t: PreLocaleSelector,
    ) -> None:
        self._url = url
        self._raw_url = raw_url
        self._reporter = reporter
        self._parse_result = parse_result
        self._platform_id = platform_id
        self._singleflight = singleflight
        self._skip_media_processing = skip_media_processing
        self._skip_download_threshold = skip_download_threshold
        self._gif_threshold = gif_only_skip_download_count_threshold
        self._save_metadata = save_metadata
        self._flyinglife_parse_result = flyinglife_parse_result
        self._skip_flyinglife = skip_flyinglife
        self._t = _t
        self._local: ParsePipeline | None = None
        self._result: PipelineResult | None = None
        self._waited = False
        self._owns_inflight = False

    def __enter__(self) -> "HybridParsePipeline":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._local is not None:
            self._local.__exit__(exc_type, exc_val, exc_tb)
        elif self._result is not None:
            self._result.cleanup()
        self.finish()

    @property
    def waited(self) -> bool:
        return self._waited or bool(self._local and self._local.waited)

    def finish(self) -> None:
        if not self._owns_inflight:
            return
        event = _inflight.pop(self._raw_url, None)
        if event is not None:
            event.set()
        self._owns_inflight = False

    async def run(self) -> PipelineResult | None:
        use_flyinglife = not self._skip_flyinglife and flyinglife.can_attempt(self._platform_id)
        if not use_flyinglife:
            return await self._run_local(singleflight=self._singleflight)

        if self._singleflight:
            existing = _inflight.get(self._raw_url)
            if existing is not None:
                self._waited = True
                logger.debug(f"FlyingLife singleflight 命中: url={self._raw_url}")
                await self._reporter.report(self._t("已有相同任务正在解析, 等待解析完成..."))
                await existing.wait()
                await self._reporter.dismiss()
                return None
            _inflight[self._raw_url] = asyncio.Event()
            self._owns_inflight = True

        try:
            remote = await flyinglife.run(
                self._url,
                self._raw_url,
                self._reporter,
                skip_media_processing=self._skip_media_processing,
                save_metadata=self._save_metadata,
                _t=self._t,
                prepared_result=self._flyinglife_parse_result,
            )
        except Exception as e:
            logger.warning(f"FlyingLife 失败, fallback ParseHub: {type(e).__name__}: {e}")
            result = await self._run_local(singleflight=False)
            if result is None:
                self.finish()
            return result

        self._result = PipelineResult(
            parse_result=remote.parse_result,
            processed_list=remote.processed_list,
            output_dir=remote.output_dir,
            engine="flyinglife",
        )
        return self._result

    async def _run_local(self, *, singleflight: bool) -> PipelineResult | None:
        self._local = ParsePipeline(
            self._url,
            self._raw_url,
            self._reporter,
            parse_result=self._parse_result,
            singleflight=singleflight,
            skip_media_processing=self._skip_media_processing,
            skip_download_threshold=self._skip_download_threshold,
            gif_only_skip_download_count_threshold=self._gif_threshold,
            save_metadata=self._save_metadata,
            _t=self._t,
        )
        self._local.__enter__()
        result = await self._local.run()
        self._result = result
        return result

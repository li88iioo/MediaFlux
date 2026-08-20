"""本地与光鸭媒体规格探测及稳定缓存。"""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from app import database as db
from app.logger import get_logger, redact_sensitive_text

logger = get_logger(__name__)


def resolve_ffprobe_executable() -> str:
    """解析 ffprobe 可执行文件；安装包显式路径优先于系统 PATH。"""
    for key in ("MEDIAFLUX_FFPROBE",):
        configured = str(os.environ.get(key) or "").strip()
        if configured:
            return str(Path(configured).expanduser())
    return shutil.which("ffprobe") or "ffprobe"


def _sanitize_probe_error(value: object, *, limit: int = 320) -> str:
    """保留可诊断信息，但移除 signedURL、查询参数和凭据。"""
    text = redact_sensitive_text(value)
    text = re.sub(r"(?i)https?://\S+", "[URL]", text)
    text = " ".join(str(text or "").split())
    return text[: max(40, int(limit))]


def _transient_probe_error(exc: subprocess.CalledProcessError) -> bool:
    evidence = f"{getattr(exc, 'stderr', '')} {getattr(exc, 'stdout', '')}".casefold()
    markers = (
        "http error 401", "http error 403", "http error 404",
        "http error 429", "http error 500", "http error 502",
        "http error 503", "http error 504", "server returned",
        "connection reset", "connection timed out", "connection refused",
        "temporary failure", "network is unreachable", "tls", "ssl",
        "i/o error", "input/output error", "end of file",
    )
    return any(marker in evidence for marker in markers)


class _ProbeCancelled(RuntimeError):
    """内部控制流：正在运行的 ffprobe 已因任务取消而终止。"""


def _terminate_probe_process(process: subprocess.Popen) -> tuple[str, str]:
    """跨平台回收 ffprobe；先温和终止，短暂等待后强制结束。"""
    try:
        process.terminate()
    except OSError:
        pass
    try:
        stdout, stderr = process.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        stdout, stderr = process.communicate()
    return str(stdout or ""), str(stderr or "")


def _run_ffprobe(
    executable: str,
    url: str,
    timeout: float,
    *,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess:
    """运行 ffprobe，并允许整理任务在子进程执行期间及时取消。"""
    command = [
        executable, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", url,
    ]
    timeout_seconds = max(0.1, float(timeout))
    if cancel_event is None:
        return subprocess.run(
            command, capture_output=True, text=True,
            timeout=timeout_seconds, check=True,
        )

    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _terminate_probe_process(process)
            raise _ProbeCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr = _terminate_probe_process(process)
            raise subprocess.TimeoutExpired(
                command, timeout_seconds, output=stdout, stderr=stderr,
            )
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    completed = subprocess.CompletedProcess(
        command, int(process.returncode or 0), stdout, stderr,
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, command, output=stdout, stderr=stderr,
        )
    return completed




class _CompositeCancelEvent:
    """把调用方取消与批次内部截止信号合并为只读 Event 接口。"""

    def __init__(self, *events: threading.Event | None):
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


# worker 睡满剩余预算醒来时，浮点误差可能让 remaining 剩下亚毫秒级正值，
# 把“预算耗尽”误判成“文件真超时”而污染失败缓存。
_BUDGET_EXHAUSTED_EPSILON_SECONDS = 0.05


class ProbeBudget:
    def __init__(self, attempts: int = 24, max_seconds: float | None = None):
        self.remaining = max(0, int(attempts))
        self.attempted = 0
        self.failure_cache_hits = 0
        self.skipped_by_budget = 0
        self.timeouts = 0
        seconds = float(max_seconds) if max_seconds is not None else 0.0
        self._deadline = time.monotonic() + seconds if seconds > 0 else None
        self._lock = threading.Lock()

    def remaining_seconds(self) -> float | None:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def consume(self) -> bool:
        with self._lock:
            remaining_seconds = self.remaining_seconds()
            if self.remaining <= 0 or (remaining_seconds is not None and remaining_seconds <= 0):
                self.skipped_by_budget += 1
                return False
            self.remaining -= 1
            self.attempted += 1
            return True

    def record_failure_cache_hit(self) -> None:
        with self._lock:
            self.failure_cache_hits += 1

    def record_timeout(self) -> None:
        with self._lock:
            self.timeouts += 1

    def clamp_timeout(
        self, requested: float, *, minimum: float = 0.1, maximum: float = 30.0
    ) -> float:
        timeout = min(max(float(minimum), float(requested)), float(maximum))
        remaining_seconds = self.remaining_seconds()
        if remaining_seconds is None:
            return timeout
        if remaining_seconds <= 0:
            return 0.0
        return min(timeout, remaining_seconds)


@dataclass(frozen=True)
class MediaProfile:
    resolution: str = ""
    dynamic_range: str = ""
    video_codec: str = ""
    bit_depth: str = ""
    fps: str = ""
    audio_codec: str = ""
    audio_channels: str = ""
    source: str = ""
    dolby_vision: bool | None = None
    atmos: bool | None = None
    # 新增字段追加在历史字段之后，保持外部位置参数构造兼容。
    video_bitrate_bps: int = 0
    overall_bitrate_bps: int = 0
    bitrate_source: str = ""

    def render(self) -> str:
        return ".".join(value for value in (
            self.source, self.resolution, self.dynamic_range, self.video_codec, self.bit_depth,
            _render_bitrate(self.video_bitrate_bps), self.fps,
            self.audio_codec, self.audio_channels,
        ) if value)


def _positive_int(value) -> int:
    try:
        parsed = int(float(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _render_bitrate(value: int) -> str:
    bitrate = _positive_int(value)
    if not bitrate:
        return ""
    mbps = bitrate / 1_000_000
    if mbps >= 1:
        text = f"{mbps:.1f}".rstrip("0").rstrip(".")
        return f"{text}Mbps"
    kbps = bitrate / 1_000
    text = f"{kbps:.0f}"
    return f"{text}Kbps"


def infer_media_source(value: object) -> str:
    """从文件名/路径提取发行来源；只识别强证据，避免把 UHD 等规格误当来源。"""
    text = str(value or "").casefold()
    checks = (
        (r"(?:^|[^a-z0-9])(?:bd|blu[ ._-]?ray)[ ._-]?remux(?:[^a-z0-9]|$)|(?:^|[^a-z0-9])remux(?:[^a-z0-9]|$)", "Remux"),
        (r"(?:^|[^a-z0-9])web[ ._-]?dl(?:[^a-z0-9]|$)", "WEB-DL"),
        (r"(?:^|[^a-z0-9])web[ ._-]?rip(?:[^a-z0-9]|$)", "WEBRip"),
        (r"(?:^|[^a-z0-9])(?:blu[ ._-]?ray|bdrip|bd[ ._-]?rip)(?:[^a-z0-9]|$)", "BluRay"),
        (r"(?:^|[^a-z0-9])hdtv(?:[^a-z0-9]|$)", "HDTV"),
        (r"(?:^|[^a-z0-9])dvd(?:rip)?(?:[^a-z0-9]|$)", "DVD"),
    )
    for pattern, label in checks:
        if re.search(pattern, text, flags=re.I):
            return label
    return ""


def _resolution(width, height) -> str:
    try:
        width, height = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return ""
    if width >= 3800 or height >= 2100:
        return "2160p"
    if width >= 1900 or height >= 1050:
        return "1080p"
    if width >= 1260 or height >= 700:
        return "720p"
    if height:
        return f"{height}p"
    return ""


def _fps(value) -> str:
    try:
        number = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return ""
    if number <= 0:
        return ""
    text = str(int(round(number))) if abs(number - round(number)) < 0.02 else f"{number:.3f}".rstrip("0").rstrip(".")
    return f"{text}fps"


def _bit_depth(video: dict) -> str:
    """读取可信位深；8-bit 不额外写入名称，避免普通媒体发生无意义重命名。"""
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            value = int(str(video.get(key) or "0").strip())
        except (TypeError, ValueError):
            value = 0
        if value > 8:
            return f"{value}-bit"
    pixel_format = str(video.get("pix_fmt") or "").strip().lower()
    match = re.search(r"p0?(10|12|14|16)(?:le|be)?$", pixel_format)
    return f"{match.group(1)}-bit" if match else ""


def _dolby_vision_profile(video_evidence: str) -> str:
    for pattern in (
        r'"dv_profile"\s*:\s*"?(\d+)',
        r'"dovi_profile"\s*:\s*"?(\d+)',
        r"(?:dvhe|dvh1|dvav|dva1)\.(\d{2})",
    ):
        match = re.search(pattern, video_evidence, flags=re.I)
        if match:
            try:
                return str(int(match.group(1)))
            except (TypeError, ValueError):
                continue
    return ""


def parse_ffprobe_payload(payload: dict, *, source_hint: object = "") -> MediaProfile:
    streams = payload.get("streams") if isinstance(payload, dict) else []
    streams = streams if isinstance(streams, list) else []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]

    def is_default_audio(item: dict) -> bool:
        value = (item.get("disposition") or {}).get("default")
        return str(value or "").strip().lower() in {"1", "true", "yes"}

    audio = next(
        (item for item in audio_streams if is_default_audio(item)),
        audio_streams[0] if audio_streams else {},
    )
    codec = {"hevc": "H.265", "h265": "H.265", "h264": "H.264", "avc": "H.264", "av1": "AV1", "vp9": "VP9"}.get(str(video.get("codec_name") or "").lower(), "")
    video_evidence = json.dumps(video, ensure_ascii=False, sort_keys=True).lower()
    audio_evidence = [
        json.dumps(item, ensure_ascii=False, sort_keys=True).lower()
        for item in audio_streams
    ]
    dolby_vision = True if any(token in video_evidence for token in (
        "dolby vision", "dovi configuration record", "dvhe.", "dvh1", "dvav", "dva1",
    )) else None
    transfer = str(video.get("color_transfer") or "").lower()
    primaries = str(video.get("color_primaries") or "").lower()
    if dolby_vision:
        profile_number = _dolby_vision_profile(video_evidence)
        dynamic = f"DoVi P{profile_number}" if profile_number else "DoVi"
    elif any(token in video_evidence for token in (
        "hdr10+", "hdr10plus", "smpte2094-40", "dynamic hdr plus",
    )):
        dynamic = "HDR10+"
    elif transfer in {"smpte2084", "smpte2084-10"}:
        dynamic = "HDR10"
    elif transfer in {"arib-std-b67", "hlg"}:
        dynamic = "HLG"
    elif transfer in {"bt709", "iec61966-2-1"} or primaries == "bt709":
        dynamic = "SDR"
    else:
        dynamic = ""

    def has_explicit_atmos(evidence: str) -> bool:
        without_negative = re.sub(r"non[^a-z0-9]*atmos", "", evidence, flags=re.I)
        return (
            bool(re.search(r"(?:^|[^a-z0-9])atmos(?:[^a-z0-9]|$)", without_negative))
            or bool(re.search(r"(?:^|[^a-z0-9])joc(?:[^a-z0-9]|$)", without_negative))
        )

    atmos = True if any(has_explicit_atmos(item) for item in audio_evidence) else None
    audio_codec = {"aac": "AAC", "eac3": "EAC3", "ac3": "AC3", "truehd": "TrueHD", "dts": "DTS", "flac": "FLAC"}.get(str(audio.get("codec_name") or "").lower(), "")
    try:
        channels = int(audio.get("channels") or 0)
    except (TypeError, ValueError):
        channels = 0
    layout = str(audio.get("channel_layout") or "").lower()
    channel_text = "2.0" if channels == 2 or layout == "stereo" else "1.0" if channels == 1 else "5.1" if channels == 6 else "7.1" if channels == 8 else ""
    format_info = payload.get("format") if isinstance(payload, dict) else {}
    format_info = format_info if isinstance(format_info, dict) else {}
    video_bitrate = _positive_int(video.get("bit_rate"))
    overall_bitrate = _positive_int(format_info.get("bit_rate"))
    return MediaProfile(
        resolution=_resolution(video.get("width"), video.get("height")),
        dynamic_range=dynamic,
        video_codec=codec,
        bit_depth=_bit_depth(video),
        fps=_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        audio_codec=audio_codec,
        audio_channels=channel_text,
        source=infer_media_source(source_hint),
        video_bitrate_bps=video_bitrate,
        overall_bitrate_bps=overall_bitrate,
        bitrate_source="video_stream" if video_bitrate else ("container" if overall_bitrate else ""),
        dolby_vision=dolby_vision,
        atmos=atmos,
    )


def media_profile_from_cache(
    payload: str, *, source_hint: object | None = None,
) -> MediaProfile | None:
    if not payload:
        return None
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("_media_probe_cache") == "failure":
            return None
        profile = MediaProfile(**data)
        if source_hint is not None:
            # 来源标签来自当前路径而非媒体比特流。内容指纹缓存可以跨 file_id
            # 复用规格，但不能把旧文件名中的 WEB-DL/Remux 等标签带到新名称。
            profile = replace(profile, source=infer_media_source(source_hint))
        return profile
    except (TypeError, ValueError):
        return None


def _failure_cache_active(payload: str) -> bool:
    if not payload:
        return False
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("_media_probe_cache") != "failure":
        return False
    try:
        retry_after = float(data.get("retry_after_epoch") or 0)
    except (TypeError, ValueError):
        return False
    return retry_after > time.time()


def _read_probe_cache(
    file_id: str, etag: str, size: int, *, allow_fingerprint_fallback: bool = False
) -> str:
    """缓存不可用时直接降级，媒体探测本身不能因迁移/磁盘异常阻断整理。"""
    try:
        return db.get_media_probe_cache(
            str(file_id), str(etag or ""), int(size or 0),
            allow_fingerprint_fallback=allow_fingerprint_fallback,
        )
    except Exception as exc:
        logger.warning(
            "读取媒体探测缓存失败 file=%s type=%s",
            str(file_id or "")[:96], type(exc).__name__,
        )
        return ""


def _write_success_cache(file_id: str, etag: str, size: int, profile: MediaProfile) -> None:
    """成功探测结果即使缓存写入失败也照常用于本次命名。"""
    try:
        db.upsert_media_probe_cache(
            str(file_id), str(etag or ""), int(size or 0),
            json.dumps(asdict(profile), ensure_ascii=False),
        )
    except Exception as exc:
        logger.warning(
            "写入媒体探测成功缓存失败 file=%s type=%s",
            str(file_id or "")[:96], type(exc).__name__,
        )


def _write_failure_cache(file, reason: str, ttl_seconds: int = 600) -> None:
    payload = json.dumps({
        "_media_probe_cache": "failure",
        "retry_after_epoch": time.time() + max(30, int(ttl_seconds)),
        "reason": str(reason or "probe_failed")[:120],
    }, ensure_ascii=False)
    try:
        db.upsert_media_probe_failure_cache(
            str(file.file_id), str(file.etag or ""), int(file.size or 0), payload,
        )
    except Exception as exc:
        logger.warning(
            "写入媒体探测失败缓存失败 file=%s type=%s",
            str(getattr(file, "file_id", "") or ""), type(exc).__name__,
        )


def _probe_should_abort(
    budget: ProbeBudget | None,
    cancel_event: threading.Event | None,
) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        return True
    if budget is None:
        return False
    remaining = budget.remaining_seconds()
    return remaining is not None and remaining <= 0


def _record_expired_budget_timeout(budget: ProbeBudget | None) -> None:
    """预算因墙钟截止而拒绝工作时，保留超时可观测性。"""
    if budget is None:
        return
    remaining = budget.remaining_seconds()
    if remaining is not None and remaining <= 0:
        budget.record_timeout()


def _acquire_download_url_lock(
    lock: threading.Lock,
    *,
    budget: ProbeBudget | None,
    cancel_event: threading.Event | None,
) -> bool:
    """可响应取消与墙钟预算地等待签名 URL 串行锁。"""
    while not _probe_should_abort(budget, cancel_event):
        wait_seconds = 0.05
        if budget is not None:
            remaining = budget.remaining_seconds()
            if remaining is not None:
                wait_seconds = min(wait_seconds, max(0.0, remaining))
        if wait_seconds <= 0:
            return False
        if lock.acquire(timeout=wait_seconds):
            if _probe_should_abort(budget, cancel_event):
                lock.release()
                return False
            return True
    return False


def _get_download_url_with_timeout(client, file_id: str, timeout: float):
    """为正式客户端传递 transport timeout，并兼容旧测试/插件桩。"""
    method = client.get_download_url
    signature_target = getattr(method, "side_effect", None)
    if not callable(signature_target):
        signature_target = method
    try:
        parameters = inspect.signature(signature_target).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    parameter_names = {parameter.name for parameter in parameters}
    supports_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    kwargs = {}
    if "timeout" in parameter_names or supports_kwargs:
        kwargs["timeout"] = timeout
    # 仅正式客户端显式声明该参数时启用，避免改变旧插件桩的调用契约。
    if "raise_timeout" in parameter_names:
        kwargs["raise_timeout"] = True
    return method(file_id, **kwargs)


def probe_media_profile(
    file,
    client,
    *,
    enabled: bool,
    timeout: int = 30,
    cache_only: bool = False,
    prefetched_payload: str | None = None,
    cache_prefetched: bool = False,
    budget: ProbeBudget | None = None,
    download_url_lock: threading.Lock | None = None,
    cancel_event: threading.Event | None = None,
) -> MediaProfile | None:
    if not enabled:
        return None
    cached = (
        str(prefetched_payload or "")
        if cache_prefetched
        else _read_probe_cache(
            str(file.file_id),
            str(file.etag or ""),
            int(file.size or 0),
            allow_fingerprint_fallback=True,
        )
    )
    profile = media_profile_from_cache(cached, source_hint=file.name)
    if profile is not None:
        return profile
    if _failure_cache_active(cached):
        if budget is not None:
            budget.record_failure_cache_hit()
        return None
    # 无副作用预览只读取历史探测缓存，不为每个云盘文件获取下载地址并
    # 串行运行 ffprobe；正式整理仍按配置执行在线探测并回填缓存。
    if cache_only:
        return None
    if cancel_event is not None and cancel_event.is_set():
        return None
    if budget is not None and not budget.consume():
        return None

    executable = resolve_ffprobe_executable()
    attempts = 0
    last_reason = "probe_failed"
    last_detail = ""
    last_returncode: int | None = None
    started = time.monotonic()
    while attempts < 2:
        if cancel_event is not None and cancel_event.is_set():
            return None
        attempts += 1
        probe_timeout = float(min(max(5, int(timeout)), 30))
        if budget is not None:
            probe_timeout = budget.clamp_timeout(probe_timeout)
        if probe_timeout <= 0:
            _record_expired_budget_timeout(budget)
            return None
        try:
            if download_url_lock is None:
                if _probe_should_abort(budget, cancel_event):
                    return None
                active_timeout = (
                    budget.clamp_timeout(probe_timeout)
                    if budget is not None else probe_timeout
                )
                if active_timeout <= 0:
                    _record_expired_budget_timeout(budget)
                    return None
                url = _get_download_url_with_timeout(
                    client, str(file.file_id), active_timeout
                )
            else:
                if not _acquire_download_url_lock(
                    download_url_lock, budget=budget, cancel_event=cancel_event
                ):
                    _record_expired_budget_timeout(budget)
                    return None
                try:
                    # 锁等待会消耗全局墙钟预算；必须在真正发起 transport
                    # 前重新计算 timeout，不能复用排队前的旧值。
                    active_timeout = (
                        budget.clamp_timeout(probe_timeout)
                        if budget is not None else probe_timeout
                    )
                    if active_timeout <= 0:
                        _record_expired_budget_timeout(budget)
                        return None
                    url = _get_download_url_with_timeout(
                        client, str(file.file_id), active_timeout
                    )
                finally:
                    download_url_lock.release()
            if cancel_event is not None and cancel_event.is_set():
                return None
            if not url:
                if budget is not None:
                    remaining_seconds = budget.remaining_seconds()
                    if remaining_seconds is not None and remaining_seconds <= 0:
                        budget.record_timeout()
                        return None
                last_reason = "download_url_unavailable"
                break
            if budget is not None:
                probe_timeout = budget.clamp_timeout(probe_timeout)
            if probe_timeout <= 0:
                _record_expired_budget_timeout(budget)
                return None
            if cancel_event is None:
                result = _run_ffprobe(executable, url, probe_timeout)
            else:
                result = _run_ffprobe(
                    executable, url, probe_timeout, cancel_event=cancel_event,
                )
            if cancel_event is not None and cancel_event.is_set():
                return None
            profile = parse_ffprobe_payload(json.loads(result.stdout or "{}"), source_hint=file.name)
            if profile.render():
                _write_success_cache(
                    str(file.file_id), str(file.etag or ""), int(file.size or 0), profile,
                )
                return profile
            last_reason = "empty_media_profile"
            break
        except _ProbeCancelled:
            return None
        except subprocess.TimeoutExpired:
            if budget is not None:
                budget.record_timeout()
            if cancel_event is not None and cancel_event.is_set():
                return None
            if budget is not None:
                remaining_seconds = budget.remaining_seconds()
                if remaining_seconds is not None and remaining_seconds <= _BUDGET_EXHAUSTED_EPSILON_SECONDS:
                    # 整理任务的总探测预算耗尽不是文件故障，不污染失败缓存。
                    return None
            last_reason = "timeout"
            last_detail = f"超过 {probe_timeout:.1f} 秒"
            break
        except TimeoutError as exc:
            if budget is not None:
                budget.record_timeout()
            if cancel_event is not None and cancel_event.is_set():
                return None
            if budget is not None:
                remaining_seconds = budget.remaining_seconds()
                if remaining_seconds is not None and remaining_seconds <= _BUDGET_EXHAUSTED_EPSILON_SECONDS:
                    # 总预算耗尽不写文件级失败缓存；由批次预算负责收敛。
                    return None
            last_reason = "timeout"
            last_detail = _sanitize_probe_error(exc)
            break
        except FileNotFoundError:
            last_reason = "ffprobe_missing"
            last_detail = f"未找到可执行文件 {Path(executable).name or 'ffprobe'}"
            break
        except json.JSONDecodeError as exc:
            last_reason = "invalid_json"
            last_detail = f"输出不是有效 JSON（line {exc.lineno} column {exc.colno}）"
            break
        except subprocess.CalledProcessError as exc:
            last_reason = "ffprobe_exit_error"
            last_returncode = int(exc.returncode or 0)
            last_detail = _sanitize_probe_error(exc.stderr or exc.stdout or "")
            should_retry = attempts == 1 and _transient_probe_error(exc)
            if should_retry and (budget is None or budget.consume()):
                continue
            break
        except Exception as exc:
            last_reason = type(exc).__name__
            last_detail = _sanitize_probe_error(exc)
            break

    ttl_seconds = 600 if last_reason == "timeout" else 60 if last_reason == "download_url_unavailable" else 120
    _write_failure_cache(file, last_reason, ttl_seconds=ttl_seconds)
    logger.warning(
        "媒体探测失败 file=%s name=%s reason=%s returncode=%s attempts=%s elapsed=%.2fs detail=%s",
        str(getattr(file, "file_id", "") or ""),
        redact_sensitive_text(str(getattr(file, "name", "") or ""))[:160],
        last_reason,
        last_returncode if last_returncode is not None else "-",
        attempts,
        time.monotonic() - started,
        last_detail or "-",
    )
    return None


def resolve_media_probe_workers() -> int:
    """云盘媒体探测并发数；保持保守上限，避免压垮家庭网络和云盘接口。"""
    try:
        configured = int(str(os.environ.get("MEDIAFLUX_MEDIA_PROBE_WORKERS") or "4"))
    except (TypeError, ValueError):
        configured = 4
    return max(1, min(configured, 8))


def probe_media_profiles_batch(
    files: list,
    client,
    *,
    enabled: bool,
    timeout: int = 30,
    prefetched_payloads: dict[tuple[str, str, int], str] | None = None,
    cache_prefetched: bool = False,
    budget: ProbeBudget | None = None,
    max_workers: int | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, MediaProfile]:
    """有界并发探测云盘文件。

    光鸭 Client 的签名 URL 获取保守串行化；耗时最大的 ffprobe 网络读取并发执行。
    返回值按 file_id 索引，单文件失败只降级该文件，不中断整批整理。
    """
    if not enabled or not files:
        return {}
    payloads = prefetched_payloads or {}
    workers = max(1, min(int(max_workers or resolve_media_probe_workers()), 8, len(files)))
    download_url_lock = threading.Lock()
    batch_stop_event = threading.Event()
    worker_cancel_event = _CompositeCancelEvent(cancel_event, batch_stop_event)

    def run_one(file) -> tuple[str, MediaProfile | None]:
        key = (
            str(getattr(file, "file_id", "") or ""),
            str(getattr(file, "etag", "") or ""),
            int(getattr(file, "size", 0) or 0),
        )
        profile = probe_media_profile(
            file,
            client,
            enabled=True,
            timeout=timeout,
            cache_only=False,
            prefetched_payload=payloads.get(key, ""),
            cache_prefetched=cache_prefetched,
            budget=budget,
            download_url_lock=download_url_lock,
            cancel_event=worker_cancel_event,
        )
        return key[0], profile

    def should_stop() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        if budget is None:
            return False
        remaining_seconds = budget.remaining_seconds()
        return remaining_seconds is not None and remaining_seconds <= 0

    if workers == 1:
        profiles: dict[str, MediaProfile] = {}
        for file in files:
            if should_stop():
                break
            file_id, profile = run_one(file)
            if file_id and profile is not None:
                profiles[file_id] = profile
        return profiles

    # 只维持 workers 个 in-flight 任务，避免一次把大目录全部提交进线程池；
    # 取消或总预算耗尽后不会再启动尚未探测的文件。
    profiles: dict[str, MediaProfile] = {}
    iterator = iter(files)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media-probe")
    pending: set[Future] = set()

    def submit_next() -> bool:
        if should_stop():
            return False
        try:
            file = next(iterator)
        except StopIteration:
            return False
        pending.add(pool.submit(run_one, file))
        return True

    try:
        for _ in range(workers):
            if not submit_next():
                break
        while pending and not should_stop():
            wait_timeout = 0.1
            if budget is not None:
                remaining_seconds = budget.remaining_seconds()
                if remaining_seconds is not None:
                    wait_timeout = min(wait_timeout, max(0.0, remaining_seconds))
            if wait_timeout <= 0:
                break
            completed, _ = wait(
                pending, timeout=wait_timeout, return_when=FIRST_COMPLETED
            )
            if not completed:
                continue
            for future in completed:
                pending.discard(future)
                try:
                    file_id, profile = future.result()
                except Exception as exc:  # 防御性边界：单任务异常不能取消其余探测
                    logger.warning("批量媒体探测任务异常 type=%s", type(exc).__name__)
                else:
                    if file_id and profile is not None:
                        profiles[file_id] = profile
                submit_next()
    finally:
        stopped_early = should_stop()
        if stopped_early:
            batch_stop_event.set()
        for future in pending:
            future.cancel()
        # 活动 HTTP 请求由剩余 ProbeBudget 限制 transport timeout；预算或
        # 取消触发时不再让调用方为已运行 worker 二次等待。worker 返回后会
        # 看到 batch_stop_event，因此不会继续 ffprobe 或写入缓存。
        pool.shutdown(wait=not stopped_early, cancel_futures=True)
    return profiles


def probe_local_media_profile(
    path: str | Path,
    *,
    size: int,
    mtime_ns: int,
    device: int = 0,
    inode: int = 0,
    timeout: int = 30,
    budget: ProbeBudget | None = None,
) -> MediaProfile | None:
    """使用本机 ffprobe 探测本地文件；失败只降级到文件名规格，不阻断整理。"""
    media_path = Path(path)
    stable_id = (
        f"local:{int(device)}:{int(inode)}"
        if int(device or 0) > 0 and int(inode or 0) > 0
        else f"local-path:{media_path.resolve(strict=False)}"
    )
    version = f"local-mtime:{int(mtime_ns or 0)}"
    cached = _read_probe_cache(stable_id, version, int(size or 0))
    profile = media_profile_from_cache(cached, source_hint=media_path.name)
    if profile is not None:
        return profile
    if _failure_cache_active(cached):
        if budget is not None:
            budget.record_failure_cache_hit()
        return None
    if budget is not None and not budget.consume():
        return None

    executable = resolve_ffprobe_executable()
    cache_file = SimpleNamespace(
        file_id=stable_id,
        etag=version,
        size=int(size or 0),
        name=media_path.name,
    )
    started = time.monotonic()
    probe_timeout = float(min(max(5, int(timeout)), 30))
    if budget is not None:
        probe_timeout = budget.clamp_timeout(probe_timeout)
    if probe_timeout <= 0:
        return None
    try:
        result = _run_ffprobe(executable, str(media_path), probe_timeout)
        profile = parse_ffprobe_payload(json.loads(result.stdout or "{}"), source_hint=media_path.name)
        if profile.render():
            _write_success_cache(stable_id, version, int(size or 0), profile)
            return profile
        reason = "empty_media_profile"
    except subprocess.TimeoutExpired:
        if budget is not None:
            budget.record_timeout()
        reason = "timeout"
    except FileNotFoundError:
        reason = "ffprobe_missing"
    except json.JSONDecodeError:
        reason = "invalid_json"
    except subprocess.CalledProcessError as exc:
        reason = f"ffprobe_exit_{int(exc.returncode or 0)}"
    except Exception as exc:
        reason = type(exc).__name__

    _write_failure_cache(cache_file, reason, ttl_seconds=300)
    logger.warning(
        "本地媒体探测失败 file=%s reason=%s elapsed=%.2fs",
        redact_sensitive_text(media_path.name)[:160],
        reason,
        time.monotonic() - started,
    )
    return None

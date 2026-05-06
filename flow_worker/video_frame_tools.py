from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path


class LastFrameExtractError(RuntimeError):
    pass


def _ffmpeg_executable() -> str:
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return ""


def _tag_number_from_name(name: str, prefix: str = "S") -> int | None:
    match = re.search(rf"(?<![@A-Za-z0-9]){re.escape(prefix)}(\d+)(?![A-Za-z0-9])", name, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def suggested_next_frame_path(video_path: str | Path, output_dir: str | Path, *, prefix: str = "S", minimum_next: int = 2) -> Path:
    video = Path(video_path)
    out_dir = Path(output_dir)
    prefix = str(prefix or "S").strip().upper() or "S"
    pad_width = 3

    source_number = _tag_number_from_name(video.stem, prefix=prefix)
    if source_number is not None:
        next_number = source_number + 1
    else:
        numbers: list[int] = []
        if out_dir.exists():
            for path in out_dir.iterdir():
                if path.is_file():
                    number = _tag_number_from_name(path.stem, prefix=prefix)
                    if number is not None:
                        numbers.append(number)
        next_number = (max(numbers) + 1) if numbers else int(minimum_next)

    return out_dir / f"{prefix}{next_number:0{pad_width}d}.png"


def suggested_next_frame_path_for_tag(tag: str, output_dir: str | Path, *, prefix: str = "S", minimum_next: int = 2) -> Path:
    out_dir = Path(output_dir)
    prefix = str(prefix or "S").strip().upper() or "S"
    match = re.search(r"([1-9][0-9]*)", str(tag or ""))
    if match:
        next_number = int(match.group(1)) + 1
    else:
        next_number = int(minimum_next)
    return out_dir / f"{prefix}{next_number:03d}.png"


def extract_last_frame(video_path: str | Path, output_path: str | Path) -> Path:
    video = Path(video_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not video.exists():
        raise LastFrameExtractError(f"영상 파일이 없습니다: {video}")
    if not video.is_file():
        raise LastFrameExtractError(f"영상 파일이 아닙니다: {video}")

    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        raise LastFrameExtractError("ffmpeg 또는 Python 패키지 imageio-ffmpeg가 필요합니다.")

    output.parent.mkdir(parents=True, exist_ok=True)
    attempts = [
        ["-sseof", "-0.08", "-i", str(video)],
        ["-sseof", "-0.5", "-i", str(video)],
        ["-sseof", "-2", "-i", str(video)],
    ]
    last_error = ""
    for input_args in attempts:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            *input_args,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(output),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
        except Exception as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
            return output
        last_error = (result.stderr or result.stdout or "").strip()

    raise LastFrameExtractError(f"마지막 프레임 추출 실패: {last_error or video}")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Extract a Flow video last frame as the next S### image.")
    parser.add_argument("video", help="Input mp4/webm/mov video path")
    parser.add_argument("-o", "--output", help="Output image path. Defaults to next S###.png beside the video.")
    parser.add_argument("--dir", dest="output_dir", help="Directory for automatic S### output naming")
    args = parser.parse_args()

    video = Path(args.video)
    if args.output:
        output = Path(args.output)
    else:
        output_dir = Path(args.output_dir) if args.output_dir else video.parent
        output = suggested_next_frame_path(video, output_dir)
    saved = extract_last_frame(video, output)
    print(saved)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""PPTX 导出后处理：把渲染器写出的 0 字节媒体替换为真实图片字节。

用法：runtime/dev_python/Scripts/python.exe tools/fix_pptx_media.py <pptx路径> <image_manifest.json>
说明：@oai/artifact-tool 2.8.52 的 exportPptx 会为 slide.images.add 写出 0 字节媒体占位
（其内存 PNG 导出正常）。本工具按渲染顺序（image.png, image2.png, ...）把清单中的源文件
字节写回包内。同名去重的媒体由渲染器自行管理，顺序即 add 顺序。
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path


def media_names(total: int) -> list[str]:
    return ["ppt/media/image.png"] + [f"ppt/media/image{i}.png" for i in range(2, total + 1)]


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: fix_pptx_media.py <pptx> <image_manifest.json>")
        return 2
    pptx_path = Path(sys.argv[1])
    sources = [str(p) for p in json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if str(p).strip()]
    if not sources:
        print("manifest 为空，无需处理")
        return 0

    with zipfile.ZipFile(pptx_path) as zin:
        names = zin.namelist()
        existing = [n for n in names if n.startswith("ppt/media/") and not n.endswith("/")]
        # 逐个核对：只回填 0 字节或与目标不一致的媒体
        replacements: dict[str, bytes] = {}
        for index, media_name in enumerate(existing):
            source = sources[index] if index < len(sources) else ""
            if not source or not Path(source).is_file():
                continue
            current = zin.read(media_name)
            wanted = Path(source).read_bytes()
            if len(current) != len(wanted) or current != wanted:
                replacements[media_name] = wanted
    if not replacements:
        print("媒体字节均已正确，无需回填")
        return 0

    tmp = pptx_path.with_suffix(".tmp.pptx")
    with zipfile.ZipFile(pptx_path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = replacements.get(item.filename)
            zout.writestr(item, data if data is not None else zin.read(item.filename))
    shutil.move(str(tmp), str(pptx_path))
    print(f"回填完成：{len(replacements)} 个媒体文件，共 {sum(len(v) for v in replacements.values())} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())

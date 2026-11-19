"""Keep PyInstaller's OpenCV hook while omitting the unused video plugin."""

import re
from importlib.resources import files
from pathlib import Path
from runpy import run_path

upstream = files("_pyinstaller_hooks_contrib").joinpath("stdhooks", "hook-cv2.py")
configuration = run_path(str(upstream))

hiddenimports = configuration["hiddenimports"]
excludedimports = configuration["excludedimports"]
datas = configuration["datas"]
module_collection_mode = configuration["module_collection_mode"]
binaries = [
    (source, destination) for source, destination in configuration["binaries"]
    if not re.fullmatch(r"opencv_videoio_ffmpeg[0-9_]+\.dll", Path(source).name, re.IGNORECASE)
]

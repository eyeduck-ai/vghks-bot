"""PDF and OCR package boundaries for the three Windows bundles."""

OCR_MODULES = (
    "rapidocr", "onnxruntime", "cv2", "numpy", "shapely", "pyclipper",
    "omegaconf", "yaml", "flatbuffers", "google.protobuf", "google._upb",
)
PDF_MODULES = (
    "biometry_parser", "pypdfium2", "pypdfium2_raw", "PIL", "pydantic",
    "pydantic_core", "psutil", "annotated_types", "typing_inspection",
)

EXCLUDED_MODULES = {"full": (), "lite": OCR_MODULES, "basic": (*OCR_MODULES, *PDF_MODULES)}

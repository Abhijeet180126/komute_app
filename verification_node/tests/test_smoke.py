def test_imports():
    import cv2, onnxruntime, zxingcpp, rapidfuzz  # noqa: F401
    from zoneinfo import ZoneInfo

    ZoneInfo("America/Toronto")

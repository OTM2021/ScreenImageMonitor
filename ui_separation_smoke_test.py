from pathlib import Path


def main() -> None:
    setup_text = Path("screen_setup_gui.py").read_text(encoding="utf-8")
    main_text = Path("screen_image_monitor_gui.py").read_text(encoding="utf-8")

    image_mode_message = (
        "\u6570\u5b57OCR\u306f\u5b9f\u884c\u3057\u307e\u305b\u3093"
    )
    old_detail_heading = "OCR\u751f\u30c7\u30fc\u30bf"
    new_detail_heading = "\u5224\u5b9a\u8a73\u7d30"

    assert image_mode_message in setup_text, (
        "Image-rule explanation is missing from screen_setup_gui.py"
    )
    assert old_detail_heading not in main_text, (
        "Legacy OCR-only detail heading remains in screen_image_monitor_gui.py"
    )
    assert new_detail_heading in main_text, (
        "Generic detector detail heading is missing from screen_image_monitor_gui.py"
    )

    assert "数字OCRルール追加" in setup_text
    assert "画像一致ルール追加" in setup_text
    assert "数値音通知追加" not in setup_text
    assert "画像音通知追加" not in setup_text
    assert "カウントアップ時に音通知する" in setup_text

    print("Detector UI separation and action integration check OK")


if __name__ == "__main__":
    main()

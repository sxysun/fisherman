import unittest

from fisherman.ocr import (
    _find_largest_bright_region_bbox_from_rows,
    _should_try_pdf_context,
)


class PdfAwareOCRTests(unittest.TestCase):
    def test_find_largest_bright_region_bbox_detects_page_like_region(self) -> None:
        rows = [[80 for _ in range(30)] for _ in range(20)]

        # Simulate a large bright document page in the middle-left.
        for y in range(2, 18):
            for x in range(6, 22):
                rows[y][x] = 250

        # Add a small bright region on the far right that should be ignored.
        for y in range(1, 5):
            for x in range(25, 29):
                rows[y][x] = 245

        bbox = _find_largest_bright_region_bbox_from_rows(rows)
        self.assertEqual(bbox, (6, 2, 22, 18))

    def test_find_largest_bright_region_bbox_returns_none_without_large_region(self) -> None:
        rows = [[120 for _ in range(20)] for _ in range(10)]
        rows[1][1] = 250
        rows[1][2] = 250
        rows[2][1] = 250

        self.assertIsNone(_find_largest_bright_region_bbox_from_rows(rows))

    def test_should_try_pdf_context_requires_preview_and_pdf_title(self) -> None:
        self.assertTrue(_should_try_pdf_context("Preview", "document.pdf"))
        self.assertTrue(_should_try_pdf_context("Preview", "DOCUMENT.PDF"))
        self.assertFalse(_should_try_pdf_context("Google Chrome", "document.pdf"))
        self.assertFalse(_should_try_pdf_context("Preview", "notes.txt"))
        self.assertFalse(_should_try_pdf_context(None, "document.pdf"))


if __name__ == "__main__":
    unittest.main()

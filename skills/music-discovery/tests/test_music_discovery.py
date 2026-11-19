import json
import sys
import unittest
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from music_discovery import official_tracks, rss_tracks


def feed(title, author):
    return ("<rss><channel><title>飙升榜</title><item>"
            f"<title>{escape(title)}</title><author>{escape(author)}</author>"
            "</item></channel></rss>")


class ChartTests(unittest.TestCase):
    def test_official_tracks_keep_order_and_version_in_title(self):
        rows = [
            {"name": '雨天 & "风" (Live)', "artists": [{"name": "甲"}, {"name": "乙"}],
             "album": {"name": "现场精选"}},
            {"name": "晴天", "artists": [{"name": "丙"}], "album": None},
        ]
        page = f'<textarea id="song-list-pre-data">{escape(json.dumps(rows))}</textarea>'
        first = {"title": '雨天 & "风" (Live)', "artist": "甲/乙", "album": "现场精选"}
        self.assertEqual(official_tracks(page, 1), [first])
        self.assertEqual(official_tracks(page, 3), [first, {"title": "晴天", "artist": "丙"}])

    def test_official_error_page_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "缺少曲目数据"):
            official_tracks("<html>暂时不可用</html>", 5)

    def test_rss_title_and_artist_can_contain_the_separator(self):
        artist = "甲 - 乙/丙"
        self.assertEqual(rss_tracks(feed(f"雨天 - 现场版 - {artist}", artist), "飙升榜", 5),
                         [{"title": "雨天 - 现场版", "artist": artist}])

    def test_rss_can_recover_title_when_only_artist_suffix_is_truncated(self):
        artist = "Justice/Tame Impala/SZA/Grand Theft Auto VI"
        xml = feed("Flower In A Hurricane - Justice/Tame Impala/SZA/Grand Theft Au...", artist)
        self.assertEqual(rss_tracks(xml, "飙升榜", 5), [{"title": "Flower In A Hurricane", "artist": artist}])
        with self.assertRaisesRegex(ValueError, "完整歌名"):
            rss_tracks(feed("Flower In A...", artist), "飙升榜", 5)

    def test_rss_rejects_a_different_chart(self):
        with self.assertRaisesRegex(ValueError, "所请求的榜单"):
            rss_tracks(feed("晴天 - 丙", "丙"), "热歌榜", 5)


if __name__ == "__main__":
    unittest.main()

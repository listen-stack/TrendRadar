from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from personal_radar import (
    classify,
    create_digest,
    deliver,
    merge_pending,
    read_pending,
    send_message,
    sent_entries,
    write_pending,
)
from trendradar.core import load_config, load_frequency_words


def article(index, **overrides):
    return {
        "title": f"Python 发布安全更新编号 {index}",
        "url": f"https://example.com/releases/{index}",
        "source_id": "python",
        "source_name": "Python 官方",
        "feed_time": "",
        "category": "计算机、开源与防御更新",
        "queued_on": "2026-11-25",
        **overrides,
    }


class PersonalRadarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_frequency_words()
        cls.config = load_config()

    def test_included_titles(self):
        expected = {
            "美联储公布最新利率决议。": "宏观经济",
            "国家统计局公布CPI数据。": "宏观经济",
            "Linux 内核发布安全更新。": "计算机、开源与防御更新",
            "Python 官方修复安全漏洞。": "计算机、开源与防御更新",
            "OpenAI 发布新模型。": "人工智能与模型",
            "DeepSeek 开源模型权重。": "人工智能与模型",
            "NASA 成功发射科研卫星。": "科学与前沿技术",
            "官方通报重大网络中断。": "重大突发与公共安全",
            "数据分析工具发布新版本。": "计算机、开源与防御更新",
            "企业正式公布财报。": "商业与产业",
            "官方否认网络传闻，网传消息不实。": "事实澄清",
            "网传企业停产？监管机构澄清相关传闻。": "事实澄清",
            "欧洲央行发布经济增长预测与情景分析": "官方预测与情景（预测）",
            "Federal Reserve Board releases economic projections from the FOMC meeting": "官方预测与情景（预测）",
            "Python 源码分析与编程入门教程": "技术学习与研究解读",
            "How to build and maintain an agentic evaluation": "技术学习与研究解读",
            "Data-based commentary on the economic outlook from independent researchers": "有依据的分析与观点",
            "基于就业调查数据的经济政策评论": "有依据的分析与观点",
            "Linux kernel security update fixes a memory flaw": "计算机、开源与防御更新",
            "某地7.2级地震": "重大突发与公共安全",
            "M 6.2 - 18 km N of Example": "重大突发与公共安全",
            "United Nations confirms ceasefire agreement": "国际与公共政策",
        }
        for title, category in expected.items():
            with self.subTest(title=title):
                self.assertEqual(classify(title, self.rules), category)

    def test_excluded_titles(self):
        titles = [
            "专家预测未来AI将取代90%的工作。",
            "某博主称新模型吊打全球。",
            "震惊！某公司背后的真相。",
            "某明星凌晨发文引发全网热议。",
            "网传某企业即将倒闭。",
            "某股票即将暴涨的内幕消息。",
            "明星电影发布会公布最新阵容。",
            "网传某地大规模断网。",
            "Unconfirmed reports of a major data breach",
            "You won't believe this Python release",
            "AI releases explained: Get rich quick",
            "股票跟单躺赚，美联储发布最新利率决议。",
            "某企业宣布普通门店周年促销。",
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assertIsNone(classify(title, self.rules))

    def test_incremental_settings(self):
        self.assertEqual(self.config["REPORT_MODE"], "incremental")
        self.assertEqual(self.config["FILTER"]["METHOD"], "keyword")
        for feature in ("AI_ANALYSIS", "AI_TRANSLATION", "WEB_SEARCH"):
            self.assertFalse(self.config[feature]["ENABLED"], feature)
        self.assertFalse(self.config["SCHEDULE"]["enabled"])
        self.assertFalse(self.config["ENABLE_CRAWLER"])

    def test_dedup_normalized_url_and_title(self):
        original = article(1)
        tracking_link = article(2, url=original["url"] + "?utm_source=rss#ref")
        changed_link = article(3, title=original["title"] + "！")
        same_event_edited_title = article(4, title="Multiple security fixes in the latest Python builds")
        self.assertEqual(
            merge_pending([], [original, tracking_link, changed_link, same_event_edited_title], []),
            [original, same_event_edited_title],
        )
        self.assertEqual(merge_pending([], [original], [original]), [])

    def test_queue_survives_failed_delivery_and_reopen(self):
        today = date(2026, 11, 25)
        items = [article(index) for index in range(14)]
        items.append(article(20, category="重大突发与公共安全"))
        with TemporaryDirectory() as directory:
            state = Path(directory)
            write_pending(state, items)
            with patch("personal_radar.send_message", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    deliver(state, self.config, self.rules, today)
            self.assertEqual(read_pending(Path(directory)), items)
            with patch("personal_radar.send_message") as send:
                deliver(Path(directory), self.config, self.rules, today)
                message, = send.call_args.args
                self.assertIn("另有 3 条留待后续推送", message)
                self.assertLess(message.find(items[-1]["title"]), message.find(items[0]["title"]))
                self.assertNotIn("源标记时间", message)
            tomorrow = today + timedelta(days=1)
            sent = list(sent_entries(Path(directory), tomorrow))
            remaining = read_pending(Path(directory))
            self.assertEqual(len(sent), 12)
            self.assertEqual(len(remaining), 3)
            self.assertEqual(merge_pending(remaining, items, sent), remaining)
            with patch("personal_radar.send_message") as send:
                deliver(state, self.config, self.rules, tomorrow)
                deliver(state, self.config, self.rules, tomorrow)
                send.assert_called_once()

    def test_message_escapes_source_fields_and_keeps_link(self):
        entry = article(1, title="Python <security> & fixes", feed_time="2026-11-25T03:40:00+00:00")
        message, selected = create_digest([entry], self.config, self.rules[0])
        self.assertEqual(selected, [entry])
        self.assertIn("Python &lt;security&gt; &amp; fixes", message)
        self.assertIn('href="https://example.com/releases/1"', message)
        self.assertIn("11-25 11:40", message)

    def test_telegram_error_does_not_print_credentials(self):
        token = "test-token-that-must-not-be-printed"
        response = Mock(status_code=401)
        response.json.return_value = {"ok": False, "description": token}
        with patch.dict("os.environ", TELEGRAM_BOT_TOKEN=token, TELEGRAM_CHAT_ID="-1001234567890"):
            with patch("personal_radar.requests.post", return_value=response):
                with patch("sys.stdout", new_callable=StringIO) as output:
                    with self.assertRaises(SystemExit) as error:
                        send_message("test")
        self.assertNotIn(token, str(error.exception))
        self.assertNotIn(token, output.getvalue())

    def test_telegram_rejects_public_channels_before_sending(self):
        with patch.dict("os.environ", TELEGRAM_BOT_TOKEN="test", TELEGRAM_CHAT_ID="-1997852516352"):
            with patch("personal_radar.telegram_call", return_value={"type": "channel", "username": "public"}) as call:
                with self.assertRaises(SystemExit):
                    send_message("test")
        call.assert_called_once_with("test", "getChat", {"chat_id": "-1997852516352"})


if __name__ == "__main__":
    unittest.main()

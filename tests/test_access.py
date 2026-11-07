import unittest

from astrbot_plugin_portfolio.models import Owner
from astrbot_plugin_portfolio.services.access import read_owner


class PublicReadAccessTests(unittest.TestCase):
    def setUp(self):
        self.b = Owner('qq-instance', 'group-A', 'user-B')

    def test_self_lookup(self):
        self.assertEqual(read_owner(self.b), self.b)

    def test_cross_user_read_keeps_actor_group(self):
        self.assertEqual(
            read_owner(self.b, 'user-A'),
            Owner('qq-instance', 'group-A', 'user-A'),
        )

    def test_structured_mention_read(self):
        # The real adapter boundary reads At.qq from event.message_obj.message.
        self.assertEqual(
            read_owner(self.b, '@group nickname', ['bot-id', 'user-A'], 'bot-id').user_id,
            'user-A',
        )

    def test_bot_mention_only_returns_actor(self):
        self.assertEqual(read_owner(self.b, '', ['bot-id'], 'bot-id'), self.b)

    def test_unstructured_at_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '无法识别'):
            read_owner(self.b, '@nick')

    def test_conflicting_explicit_id_and_at_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '不同'):
            read_owner(self.b, 'user-C', ['user-A'])

    def test_multiple_mentions_rejected(self):
        with self.assertRaisesRegex(ValueError, '只能查询一位'):
            read_owner(self.b, '', ['user-A', 'user-C'])

    def test_same_mention_deduplicates(self):
        self.assertEqual(read_owner(self.b, '', ['user-A', 'user-A']).user_id, 'user-A')

    def test_empty_or_incomplete_actor_rejected(self):
        with self.assertRaisesRegex(ValueError, '身份'):
            read_owner(Owner('qq-instance', '', 'user-B'), 'user-A')

    def test_invalid_target_rejected(self):
        with self.assertRaisesRegex(ValueError, '格式'):
            read_owner(self.b, 'user\nA')


if __name__ == '__main__':
    unittest.main()

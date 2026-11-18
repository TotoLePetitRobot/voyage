from unittest import mock

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from .models import User

PW = "Tr0ub4dor&3xyz"


class AccountTests(TestCase):
    def test_username_is_unique_ignoring_case(self):
        User.objects.create_user("Marie", password=PW)
        r = self.client.post(reverse("signup"), {"username": "marie"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(User.objects.count(), 1)

    def test_login_ignores_case(self):
        User.objects.create_user("Marie", password=PW)
        self.assertTrue(self.client.login(username="marie", password=PW))

    def test_password_reset_only_mails_matching_account(self):
        User.objects.create_user("marie", email="marie@example.com", password=PW)
        self.client.post(reverse("password_reset"), {"email": "marie@example.com"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("marie", mail.outbox[0].body)
        self.client.post(reverse("password_reset"), {"email": "inconnu@example.com"})
        self.assertEqual(len(mail.outbox), 1)

    def test_mail_failure_is_logged_and_hidden_from_visitor(self):
        User.objects.create_user("marie", email="marie@example.com", password=PW)
        with mock.patch("django.core.mail.EmailMultiAlternatives.send",
                        side_effect=OSError("boom")):
            with self.assertLogs("django.contrib.auth", level="ERROR"):
                r = self.client.post(reverse("password_reset"), {"email": "marie@example.com"})
        self.assertRedirects(r, reverse("password_reset_done"))

class PasswordOptionalSignupTests(TestCase):
    def test_signup_without_password_and_email(self):
        r = self.client.post(reverse("signup"), {"username": "paul"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(User.objects.get(username="paul").check_password(""))

    def test_signup_with_password_requires_email(self):
        r = self.client.post(reverse("signup"),
                             {"username": "marie", "password1": PW, "password2": PW})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="marie").exists())

    def test_signup_with_password_and_email(self):
        r = self.client.post(reverse("signup"), {
            "username": "marie", "email": "marie@example.com",
            "password1": PW, "password2": PW})
        self.assertEqual(r.status_code, 302)
        user = User.objects.get(username="marie")
        self.assertTrue(user.check_password(PW))
        self.assertEqual(user.email, "marie@example.com")

    def test_mismatched_passwords_are_rejected(self):
        r = self.client.post(reverse("signup"), {
            "username": "marie", "email": "marie@example.com",
            "password1": PW, "password2": PW + "x"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="marie").exists())


class PasswordOptionalLoginTests(TestCase):
    def test_login_with_empty_password(self):
        User.objects.create_user("paul", password="")
        r = self.client.post(reverse("login"), {"username": "paul", "password": ""})
        self.assertEqual(r.status_code, 302)

    def test_empty_password_does_not_open_account_with_password(self):
        User.objects.create_user("marie", email="m@example.com", password=PW)
        r = self.client.post(reverse("login"), {"username": "marie", "password": ""})
        self.assertEqual(r.status_code, 200)

    def test_staff_cannot_log_in_without_password(self):
        User.objects.create_user("boss", password="", is_staff=True)
        self.assertFalse(self.client.login(username="boss", password=""))
        r = self.client.post(reverse("login"), {"username": "boss", "password": ""})
        self.assertEqual(r.status_code, 200)


class ProfileTests(TestCase):
    def test_setting_password_requires_email(self):
        user = User.objects.create_user("paul", password="")
        self.client.force_login(user)
        r = self.client.post(reverse("profile"),
                             {"email": "", "new_password1": PW, "new_password2": PW})
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password(""))

    def test_setting_password_with_email_keeps_session(self):
        user = User.objects.create_user("paul", password="")
        self.client.force_login(user)
        r = self.client.post(reverse("profile"), {
            "email": "paul@example.com", "new_password1": PW, "new_password2": PW})
        self.assertEqual(r.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PW))
        self.assertEqual(user.email, "paul@example.com")
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)

    def test_cannot_clear_email_while_password_set(self):
        user = User.objects.create_user("marie", email="m@example.com", password=PW)
        self.client.force_login(user)
        r = self.client.post(reverse("profile"), {"email": "", "current_password": PW})
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.email, "m@example.com")

    def test_wrong_current_password_blocks_changes(self):
        user = User.objects.create_user("marie", email="m@example.com", password=PW)
        self.client.force_login(user)
        r = self.client.post(reverse("profile"),
                             {"email": "autre@example.com", "current_password": "mauvais"})
        self.assertEqual(r.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.email, "m@example.com")

    def test_remove_password(self):
        user = User.objects.create_user("marie", email="m@example.com", password=PW)
        self.client.force_login(user)
        r = self.client.post(reverse("profile"), {
            "email": "m@example.com", "current_password": PW, "remove_password": "on"})
        self.assertEqual(r.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password(""))

    def test_old_password_change_page_redirects_to_profile(self):
        user = User.objects.create_user("marie", email="m@example.com", password=PW)
        self.client.force_login(user)
        r = self.client.get(reverse("password_change"))
        self.assertRedirects(r, reverse("profile"))

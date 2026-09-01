import pytest

from app.core.users import ROLE_ADMIN, ROLE_REVIEWER, UserError, UserStore


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.json", super_admin="root")


class TestSuperAdmin:
    def test_exists_without_a_file(self, store):
        admin = store.get("root")
        assert admin is not None and admin.is_admin

    def test_cannot_be_recreated(self, store):
        with pytest.raises(UserError, match="super admin"):
            store.create("root", "password123")

    def test_cannot_be_disabled(self, store):
        with pytest.raises(UserError, match="cannot be disabled"):
            store.set_active("root", False)

    def test_is_listed_first(self, store):
        store.create("aaa", "password123")
        assert store.list()[0].username == "root"


class TestCreate:
    def test_creates_a_reviewer(self, store):
        user = store.create("mai", "password123", display_name="Mai", created_by="root")
        assert user.role == ROLE_REVIEWER
        assert user.created_by == "root"
        assert store.get("mai") is not None

    def test_usernames_are_lowercased(self, store):
        store.create("MAI", "password123")
        assert store.get("mai") is not None

    def test_survives_a_reload(self, store, tmp_path):
        store.create("mai", "password123")
        assert UserStore(tmp_path / "users.json", "root").get("mai") is not None

    def test_duplicates_are_rejected(self, store):
        store.create("mai", "password123")
        with pytest.raises(UserError, match="already exists"):
            store.create("mai", "password456")

    @pytest.mark.parametrize("name", ["ab", "_mai", "Mai Nguyen", "x" * 33, "mai@x"])
    def test_invalid_usernames_are_rejected(self, store, name):
        with pytest.raises(UserError, match="Username"):
            store.create(name, "password123")

    def test_short_passwords_are_rejected(self, store):
        with pytest.raises(UserError, match="at least 8"):
            store.create("mai", "short")

    def test_an_unknown_role_is_rejected(self, store):
        with pytest.raises(UserError, match="Role must be"):
            store.create("mai", "password123", role="superuser")

    def test_an_admin_can_be_created(self, store):
        assert store.create("boss", "password123", role=ROLE_ADMIN).is_admin


class TestAuthenticate:
    def test_accepts_the_right_password(self, store):
        store.create("mai", "password123")
        assert store.authenticate("mai", "password123") is not None

    def test_rejects_the_wrong_password(self, store):
        store.create("mai", "password123")
        assert store.authenticate("mai", "password124") is None

    def test_rejects_an_unknown_user(self, store):
        assert store.authenticate("ghost", "password123") is None

    def test_the_password_is_never_stored_in_the_clear(self, store, tmp_path):
        store.create("mai", "correcthorsebattery")
        assert "correcthorsebattery" not in (tmp_path / "users.json").read_text()

    def test_a_disabled_user_cannot_sign_in(self, store):
        store.create("mai", "password123")
        store.set_active("mai", False)
        assert store.authenticate("mai", "password123") is None

    def test_re_enabling_restores_access(self, store):
        store.create("mai", "password123")
        store.set_active("mai", False)
        store.set_active("mai", True)
        assert store.authenticate("mai", "password123") is not None

    def test_a_new_password_replaces_the_old_one(self, store):
        store.create("mai", "password123")
        store.set_password("mai", "newpassword456")
        assert store.authenticate("mai", "password123") is None
        assert store.authenticate("mai", "newpassword456") is not None

    def test_setting_a_password_on_an_unknown_user_is_rejected(self, store):
        with pytest.raises(UserError, match="No such user"):
            store.set_password("ghost", "password123")

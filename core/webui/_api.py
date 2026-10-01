"""WebUI API 门面：按域拆分在 _api_auth / _api_admin / _api_music，此模块聚合导出。"""

from __future__ import annotations

from ._api_admin import (  # noqa: F401
    make_acl_get,
    make_acl_save,
    make_config_get,
    make_config_save,
    make_overview,
    make_stats,
    make_stats_reset,
)
from ._api_auth import (  # noqa: F401
    make_change_password,
    make_check,
    make_login,
    make_logout,
    make_revoke,
    make_sessions,
)
from ._api_music import (  # noqa: F401
    make_account_cookie,
    make_account_logout,
    make_account_refresh,
    make_accounts,
    make_history,
    make_logs,
    make_preview,
    make_qr_cancel,
    make_qr_start,
    make_qr_status,
    make_remote_play,
    make_remote_scopes,
    make_resolve,
    make_search,
    make_service_check,
)

# 端点 × 拒绝用例映射表（S3）

> 产出日期：2026-11-03 · 版本：v0.17.42 · 基线回归：699 passed / 18 skipped
>
> 口径：端点以 `server/main.py`（`@app.`）与 `server/routes/*.py`（`@router.`）实际注册的装饰器为准，共 35 个。
> 「用例」列只引用**真实存在且当前通过**的测试函数（已逐个 grep 核实存在、抽 8 条实跑验证、全部落在 699 passed 内）；
> 只登记该用例**实际打到**的端点，不跨端点挪用。没有拒绝用例的如实标「**缺口**」。
> 「公开」端点（登录、健康检查、静态资源）设计上不需要鉴权，不计入缺口。

## 结论

- 35 个端点中：**4 个公开端点**（不计缺口）、**22 个有真实拒绝用例覆盖**（其中 2 个仅部分场景有覆盖，见行内备注）、**9 个纯缺口**。
- 缺口集中在两类：
  1. **管理类子端点只有共享守卫、无端点专属用例**：`admin_users.py` 里 create / rotate / password / department / revoke 五个写端点，403 由共享 `_admin_guard` 实施，但测试只打了 GET users 与 POST role 两个端点，其余五个没有任何专属拒绝用例；
  2. **列表/查询类端点缺未认证 401 专属用例**：`GET /api/approvals`、`GET /api/uploads`、`GET /api/usage`（后者连 HTTP 端点级用例都没有，`test_usage.py` 全是单元级）。
- 另有 `GET /vendor/{name}` 的路径穿越 404 防护（`path.name != name` 校验）无任何用例。

## 映射表

| # | 端点 | 鉴权 / 拒绝场景 | 用例（文件::函数） | 状态 |
|---|------|----------------|--------------------|------|
| 1 | GET `/` | 静态页，设计开放 | — | 公开 |
| 2 | GET `/console` | 静态页，设计开放 | — | 公开 |
| 3 | GET `/tokens.css` | 静态资源，设计开放 | — | 公开 |
| 4 | GET `/api/health` | 鉴权白名单；未认证仅回最小信息 `{status, auth_enabled}` | test_main_api.py::test_health_unauth_minimal | 公开（有用例佐证） |
| 5 | GET `/vendor/{name}` | 文件名白名单 + `path.name != name` 防穿越 → 404 | — | **缺口**：穿越拒绝无用例 |
| 6 | POST `/api/auth/login` | 公开入口；错误口令 → 401；失败留审计 | test_main_api.py::test_login_wrong_password_fuzzy；test_audit.py::test_ac1_login_failure_audited | 有覆盖 |
| 7 | POST `/api/auth/logout` | 登出后旧 PAT 失效 → 401 | test_main_api.py::test_logout_invalidates_pat | 部分覆盖（缺：未认证直打 logout 的 401 专属用例） |
| 8 | GET `/api/admin/users` | 未认证 → 401；非 admin → 403；开放模式 → 400 | test_admin_users.py::test_ac9_no_credentials_401；test_admin_users.py::test_ac9_user_role_403；test_admin_users.py::test_ac9_open_mode_400 | 有覆盖 |
| 9 | POST `/api/admin/users` | 未认证 → 401；非 admin → 403（共享 `_admin_guard`） | — | **缺口** |
| 10 | POST `/api/admin/users/{username}/rotate` | 同上 | — | **缺口** |
| 11 | POST `/api/admin/users/{username}/password` | 同上 | — | **缺口** |
| 12 | POST `/api/admin/users/{username}/role` | 非 admin → 403 | test_admin_users.py::test_ac9_approver_role_403 | 有覆盖（缺：401 与开放模式 400 专属用例） |
| 13 | POST `/api/admin/users/{username}/department` | 同 #9 | — | **缺口** |
| 14 | POST `/api/admin/users/{username}/revoke` | 同 #9 | — | **缺口** |
| 15 | GET `/api/admin/packs` | 未认证 → 401；user/approver → 403；开放模式 → 400 | test_packs_approvals.py::test_ac8_packs_endpoints_no_credentials_401；test_packs_approvals.py::test_ac8_packs_endpoints_user_approver_403；test_packs_approvals.py::test_ac8_packs_endpoints_open_mode_400 | 有覆盖 |
| 16 | POST `/api/admin/packs/{name}/disable` | 同 #15 三场景；平台包不可禁用 → 400；未知包 → 404 | 同 #15 三条；test_packs_approvals.py::test_ac3_platform_pack_disable_refused_400；test_packs_approvals.py::test_ac3_unknown_pack_toggle_404 | 有覆盖 |
| 17 | POST `/api/admin/packs/{name}/enable` | 同 #15 三场景；损坏包不可启用 → 400；未知包 → 404 | 同 #15 三条；test_packs_approvals.py::test_ac3_broken_pack_enable_refused_400；test_packs_approvals.py::test_ac3_unknown_pack_toggle_404 | 有覆盖 |
| 18 | GET `/api/approvals` | 未认证 → 401；无角色拒绝设计（user 仅见自己发起，属可见性过滤） | — | **缺口**：401 无专属用例（test_ac6_approvals_visibility_and_fields 是可见性用例，非拒绝用例，不引用） |
| 19 | POST `/api/approve` | 非 approver → 403（`_approver_guard`） | test_main_api.py::test_approve_role_separation；test_packs_approvals.py::test_ac8_user_approve_403 | 有覆盖 |
| 20 | POST `/api/chat` | 越权他人会话 → 403；tombstone 复用 → 403；cookie 缺 `X-Requested-With` → 403（CSRF） | test_session_acl.py::test_ac1_chat_other_user_session_403；test_session_acl.py::test_ac1_chat_approver_not_owner_403；test_session_acl.py::test_n4_mixed_legacy_session_third_party_denied；test_session_acl.py::test_ac1_chat_legacy_multi_author_second_user_403；test_session_acl.py::test_ac1_chat_race_window_second_check_rejects；test_session_lifecycle.py::test_ac1_reuse_after_delete_rejected_and_checkpoint_gone；test_auth_cookie.py::test_ac13_cookie_post_without_xrw_403 | 有覆盖（缺：未认证 401 专属用例） |
| 21 | POST `/api/uploads` | 空体 → 400；超限 → 413（含 Content-Length 预检）；非法文件名 → 400；docx 解压炸弹 → 400 | test_deny_paths.py::test_upload_empty_body_rejected；test_deny_paths.py::test_upload_oversize_rejected；test_main_api.py::test_upload_size_precheck_413；test_main_api.py::test_upload_bad_names_rejected；test_deny_paths.py::test_upload_docx_oversize_xml_rejected | 有覆盖 |
| 22 | GET `/api/files/{name:path}` | 路径穿越 → 400；跨用户域 → 404 | test_deny_paths.py::test_download_traversal_rejected；test_main_api.py::test_files_download_domain_rules | 有覆盖 |
| 23 | GET `/api/uploads`（列表） | 未认证 → 401 | — | **缺口**（test_upload_and_domain_isolation 验证跨用户不可见，属可见性过滤，非拒绝用例） |
| 24 | POST `/api/skillpacks` | 空体/超限 → 400/413；zip 条目数超限 → 400；zip 解压总量超限 → 400 | test_deny_paths.py::test_skillpack_empty_and_oversize_rejected；test_deny_paths.py::test_skillpack_zip_entries_limit；test_deny_paths.py::test_skillpack_zip_total_size_limit | 有覆盖 |
| 25 | DELETE `/api/uploads/{name:path}` | 路径穿越 → 400；单段名路由本域；user 两段名强制本域；跨用户删除 → 404 | test_deny_paths.py::test_delete_upload_traversal_rejected；test_deny_paths.py::test_delete_upload_single_segment_routes_own_scope；test_deny_paths.py::test_delete_upload_two_segment_user_forced_own_scope；test_main_api.py::test_upload_and_domain_isolation | 有覆盖 |
| 26 | POST `/api/kb/sanitize` | 未认证 → 401；超 2MB → 413（含 Content-Length 预检） | test_kb_source_xss.py::test_ac10_sanitize_auth_required；test_kb_source_xss.py::test_ac10_sanitize_size_limit；test_kb_source_xss.py::test_ac10_sanitize_content_length_precheck | 有覆盖 |
| 27 | GET `/api/kb/source` | 未认证 → 401；路径穿越 → 404；跨部门 → 404（含未分配部门） | test_kb_citation.py::test_ac6_unauthorized_401；test_kb_citation.py::test_ac6_traversal_rejected；test_kb_permissions.py::test_ac9_other_department_kb_source_404；test_kb_permissions.py::test_ac9_unassigned_department_404 | 有覆盖 |
| 28 | GET `/api/sessions` | 未认证 → 401；非受信来源 SSO 头忽略 → 401 | test_main_api.py::test_middleware_401；test_deny_paths.py::test_sso_header_from_untrusted_source_ignored | 有覆盖 |
| 29 | GET `/api/sessions/{session_id}/history` | 他人会话 → 403；tombstone → 404 | test_main_api.py::test_history_forbidden_for_other_user；test_session_lifecycle.py::test_tombstone_history_delete_404 | 有覆盖 |
| 30 | DELETE `/api/sessions/{session_id}` | 他人会话（user/approver）→ 403；tombstone → 404 | test_deny_paths.py::test_session_delete_user_forbidden；test_deny_paths.py::test_session_delete_approver_forbidden_on_others；test_session_lifecycle.py::test_tombstone_history_delete_404 | 有覆盖 |
| 31 | GET `/api/audit` | 未认证 → 401；user/approver → 403；开放模式 → 400 | test_audit.py::test_ac5_no_credentials_401；test_audit.py::test_ac5_user_403；test_audit.py::test_ac5_approver_403；test_audit.py::test_ac5_open_mode_400 | 有覆盖 |
| 32 | GET `/api/audit/export` | user → 403 | test_audit.py::test_ac5_user_403（同函数内同时打该端点） | 部分覆盖（缺：未认证 401、approver 角色专属用例） |
| 33 | GET `/api/usage` | 未认证 → 401（user 强制看本人，无 403 设计） | — | **缺口**：无任何 HTTP 端点级用例（test_usage.py 全为单元级） |
| 34 | GET `/api/usage/departments` | 非 admin → 403 | test_usage_departments.py::test_ac4_user_role_forbidden | 有覆盖 |
| 35 | GET `/api/usage/departments.csv` | 非 admin → 403 | test_usage_departments.py::test_ac4_user_role_forbidden（同函数内同时打该端点） | 有覆盖 |

## 统计

| 分类 | 数量 | 端点 |
|------|------|------|
| 公开（不计缺口） | 4 | #1–#4 |
| 有拒绝用例覆盖 | 22 | #6–#8、#12、#15–#17、#19–#22、#24–#32、#34–#35（其中 #7、#32 为部分覆盖） |
| 纯缺口 | 9 | #5、#9、#10、#11、#13、#14、#18、#23、#33 |

## 备注

- 全局中间件（`server/main.py`）对所有 `/api/*`（除 `/api/health`、`/api/auth/login`）实施统一 401，对 cookie 身份的非 GET 请求实施 CSRF 403。表内只登记**实际打到该端点**的用例；中间件级 401 只在有用例命中的端点上登记，未命中的一律标缺口或行内备注，不做"中间件已覆盖"的推定。
- 18 个 skipped 均为集成/环境门控（docker、真实 LLM、fastembed、Windows 符号链接、langfuse），与本表引用用例无交集。

# 端点 × 拒绝用例映射表（S3）

> 产出日期：2026-11-03 · 版本：v0.17.43 · 基线回归：784 passed / 18 skipped
>
> 口径：端点以 `server/main.py`（`@app.`）与 `server/routes/*.py`（`@router.`）实际注册的装饰器为准，共 35 个。
> 「用例」列只引用**真实存在且当前通过**的测试函数；只登记该用例**实际打到**的端点，不跨端点挪用。
> 2026-11-03 一期补齐后，原 9 个纯缺口与 2 个半覆盖端点已由 `tests/test_endpoint_deny_gaps.py` 补齐（新增 32 条）。

## 结论

- 35 个端点中：**4 个公开端点**（不计缺口）、**31 个有真实拒绝用例覆盖**（其中 #12、#20 仍为部分场景覆盖，见行内备注）、**0 个纯缺口**。
- 一期补齐已落地：管理端五个写端点各配 401 / 403 / 开放模式 400（并断言被拒时无副作用）；`GET /vendor/{name}` 补真实穿越路径 404；`GET /api/approvals`、`GET /api/uploads`、`GET /api/usage` 补未认证 401；`POST /api/auth/logout` 补未认证 401；`GET /api/audit/export` 补 401 与 approver 403。
- 仍存的部分覆盖：#12 管理端改角色缺未认证 401 与开放模式 400 专属用例；#20 `POST /api/chat` 缺未认证 401 专属用例（越权与 CSRF 已有）。

## 映射表

| # | 端点 | 鉴权 / 拒绝场景 | 用例（文件::函数） | 状态 |
|---|------|----------------|--------------------|------|
| 1 | GET `/` | 静态页，设计开放 | — | 公开 |
| 2 | GET `/console` | 静态页，设计开放 | — | 公开 |
| 3 | GET `/tokens.css` | 静态资源，设计开放 | — | 公开 |
| 4 | GET `/api/health` | 鉴权白名单；未认证仅回最小信息 `{status, auth_enabled}` | test_main_api.py::test_health_unauth_minimal | 公开（有用例佐证） |
| 5 | GET `/vendor/{name}` | 文件名白名单 + `path.name != name` 防穿越 → 404 | test_endpoint_deny_gaps.py::test_vendor_traversal_rejected_404（另 test_vendor_legit_file_still_served 做正向对照） | 有覆盖 |
| 6 | POST `/api/auth/login` | 公开入口；错误口令 → 401；失败留审计 | test_main_api.py::test_login_wrong_password_fuzzy；test_audit.py::test_ac1_login_failure_audited | 有覆盖 |
| 7 | POST `/api/auth/logout` | 登出后旧 PAT 失效 → 401；未认证直打 → 401 | test_main_api.py::test_logout_invalidates_pat；test_endpoint_deny_gaps.py::test_logout_no_credentials_401 | 有覆盖 |
| 8 | GET `/api/admin/users` | 未认证 → 401；非 admin → 403；开放模式 → 400 | test_admin_users.py::test_ac9_no_credentials_401；test_admin_users.py::test_ac9_user_role_403；test_admin_users.py::test_ac9_open_mode_400 | 有覆盖 |
| 9 | POST `/api/admin/users` | 未认证 → 401；user/approver → 403；开放模式 → 400 | test_endpoint_deny_gaps.py::test_admin_write_endpoint_no_credentials_401 / test_admin_write_endpoint_non_admin_403 / test_admin_write_endpoint_open_mode_400（参数化 id=create） | 有覆盖 |
| 10 | POST `/api/admin/users/{username}/rotate` | 同上 | 同 #9 三条（参数化 id=rotate）；另 test_admin_rotate_rejected_keeps_old_key_valid 断言旧 Key 未失效 | 有覆盖 |
| 11 | POST `/api/admin/users/{username}/password` | 同上 | 同 #9 三条（参数化 id=password） | 有覆盖 |
| 12 | POST `/api/admin/users/{username}/role` | 非 admin → 403 | test_admin_users.py::test_ac9_approver_role_403 | 有覆盖（缺：401 与开放模式 400 专属用例） |
| 13 | POST `/api/admin/users/{username}/department` | 同 #9 | 同 #9 三条（参数化 id=department） | 有覆盖 |
| 14 | POST `/api/admin/users/{username}/revoke` | 同 #9 | 同 #9 三条（参数化 id=revoke）；另 test_admin_revoke_rejected_keeps_user_active 断言用户仍可访问 | 有覆盖 |
| 15 | GET `/api/admin/packs` | 未认证 → 401；user/approver → 403；开放模式 → 400 | test_packs_approvals.py::test_ac8_packs_endpoints_no_credentials_401；test_packs_approvals.py::test_ac8_packs_endpoints_user_approver_403；test_packs_approvals.py::test_ac8_packs_endpoints_open_mode_400 | 有覆盖 |
| 16 | POST `/api/admin/packs/{name}/disable` | 同 #15 三场景；平台包不可禁用 → 400；未知包 → 404 | 同 #15 三条；test_packs_approvals.py::test_ac3_platform_pack_disable_refused_400；test_packs_approvals.py::test_ac3_unknown_pack_toggle_404 | 有覆盖 |
| 17 | POST `/api/admin/packs/{name}/enable` | 同 #15 三场景；损坏包不可启用 → 400；未知包 → 404 | 同 #15 三条；test_packs_approvals.py::test_ac3_broken_pack_enable_refused_400；test_packs_approvals.py::test_ac3_unknown_pack_toggle_404 | 有覆盖 |
| 18 | GET `/api/approvals` | 未认证 → 401；无角色拒绝设计（user 仅见自己发起，属可见性过滤） | test_endpoint_deny_gaps.py::test_list_endpoints_no_credentials_401（参数化 /api/approvals） | 有覆盖 |
| 19 | POST `/api/approve` | 非 approver → 403（`_approver_guard`） | test_main_api.py::test_approve_role_separation；test_packs_approvals.py::test_ac8_user_approve_403 | 有覆盖 |
| 20 | POST `/api/chat` | 越权他人会话 → 403；tombstone 复用 → 403；cookie 缺 `X-Requested-With` → 403（CSRF） | test_session_acl.py::test_ac1_chat_other_user_session_403；test_session_acl.py::test_ac1_chat_approver_not_owner_403；test_session_acl.py::test_n4_mixed_legacy_session_third_party_denied；test_session_acl.py::test_ac1_chat_legacy_multi_author_second_user_403；test_session_acl.py::test_ac1_chat_race_window_second_check_rejects；test_session_lifecycle.py::test_ac1_reuse_after_delete_rejected_and_checkpoint_gone；test_auth_cookie.py::test_ac13_cookie_post_without_xrw_403 | 有覆盖（缺：未认证 401 专属用例） |
| 21 | POST `/api/uploads` | 空体 → 400；超限 → 413（含 Content-Length 预检）；非法文件名 → 400；docx 解压炸弹 → 400 | test_deny_paths.py::test_upload_empty_body_rejected；test_deny_paths.py::test_upload_oversize_rejected；test_main_api.py::test_upload_size_precheck_413；test_main_api.py::test_upload_bad_names_rejected；test_deny_paths.py::test_upload_docx_oversize_xml_rejected | 有覆盖 |
| 22 | GET `/api/files/{name:path}` | 路径穿越 → 400；跨用户域 → 404 | test_deny_paths.py::test_download_traversal_rejected；test_main_api.py::test_files_download_domain_rules | 有覆盖 |
| 23 | GET `/api/uploads`（列表） | 未认证 → 401 | test_endpoint_deny_gaps.py::test_list_endpoints_no_credentials_401（参数化 /api/uploads） | 有覆盖 |
| 24 | POST `/api/skillpacks` | 空体/超限 → 400/413；zip 条目数超限 → 400；zip 解压总量超限 → 400 | test_deny_paths.py::test_skillpack_empty_and_oversize_rejected；test_deny_paths.py::test_skillpack_zip_entries_limit；test_deny_paths.py::test_skillpack_zip_total_size_limit | 有覆盖 |
| 25 | DELETE `/api/uploads/{name:path}` | 路径穿越 → 400；单段名路由本域；user 两段名强制本域；跨用户删除 → 404 | test_deny_paths.py::test_delete_upload_traversal_rejected；test_deny_paths.py::test_delete_upload_single_segment_routes_own_scope；test_deny_paths.py::test_delete_upload_two_segment_user_forced_own_scope；test_main_api.py::test_upload_and_domain_isolation | 有覆盖 |
| 26 | POST `/api/kb/sanitize` | 未认证 → 401；超 2MB → 413（含 Content-Length 预检） | test_kb_source_xss.py::test_ac10_sanitize_auth_required；test_kb_source_xss.py::test_ac10_sanitize_size_limit；test_kb_source_xss.py::test_ac10_sanitize_content_length_precheck | 有覆盖 |
| 27 | GET `/api/kb/source` | 未认证 → 401；路径穿越 → 404；跨部门 → 404（含未分配部门） | test_kb_citation.py::test_ac6_unauthorized_401；test_kb_citation.py::test_ac6_traversal_rejected；test_kb_permissions.py::test_ac9_other_department_kb_source_404；test_kb_permissions.py::test_ac9_unassigned_department_404 | 有覆盖 |
| 28 | GET `/api/sessions` | 未认证 → 401；非受信来源 SSO 头忽略 → 401 | test_main_api.py::test_middleware_401；test_deny_paths.py::test_sso_header_from_untrusted_source_ignored | 有覆盖 |
| 29 | GET `/api/sessions/{session_id}/history` | 他人会话 → 403；tombstone → 404 | test_main_api.py::test_history_forbidden_for_other_user；test_session_lifecycle.py::test_tombstone_history_delete_404 | 有覆盖 |
| 30 | DELETE `/api/sessions/{session_id}` | 他人会话（user/approver）→ 403；tombstone → 404 | test_deny_paths.py::test_session_delete_user_forbidden；test_deny_paths.py::test_session_delete_approver_forbidden_on_others；test_session_lifecycle.py::test_tombstone_history_delete_404 | 有覆盖 |
| 31 | GET `/api/audit` | 未认证 → 401；user/approver → 403；开放模式 → 400 | test_audit.py::test_ac5_no_credentials_401；test_audit.py::test_ac5_user_403；test_audit.py::test_ac5_approver_403；test_audit.py::test_ac5_open_mode_400 | 有覆盖 |
| 32 | GET `/api/audit/export` | 未认证 → 401；user → 403；approver → 403 | test_endpoint_deny_gaps.py::test_audit_export_no_credentials_401；test_endpoint_deny_gaps.py::test_audit_export_approver_403；test_audit.py::test_ac5_user_403（同函数内同时打该端点） | 有覆盖 |
| 33 | GET `/api/usage` | 未认证 → 401（user 强制看本人，无 403 设计） | test_endpoint_deny_gaps.py::test_list_endpoints_no_credentials_401（参数化 /api/usage） | 有覆盖 |
| 34 | GET `/api/usage/departments` | 非 admin → 403 | test_usage_departments.py::test_ac4_user_role_forbidden | 有覆盖 |
| 35 | GET `/api/usage/departments.csv` | 非 admin → 403 | test_usage_departments.py::test_ac4_user_role_forbidden（同函数内同时打该端点） | 有覆盖 |

## 统计

| 分类 | 数量 | 端点 |
|------|------|------|
| 公开（不计缺口） | 4 | #1–#4 |
| 有拒绝用例覆盖 | 31 | #5–#32、#34–#35（其中 #12、#20 仍为部分场景覆盖） |
| 纯缺口 | 0 | — |

## 备注

- 全局中间件（`server/main.py`）对所有 `/api/*`（除 `/api/health`、`/api/auth/login`）实施统一 401，对 cookie 身份的非 GET 请求实施 CSRF 403。表内只登记**实际打到该端点**的用例；中间件级 401 只在有用例命中的端点上登记，未命中的一律标缺口或行内备注，不做"中间件已覆盖"的推定。
- 18 个 skipped 均为集成/环境门控（docker、真实 LLM、fastembed、Windows 符号链接、langfuse），与本表引用用例无交集。

## 补充：编排工具级权限点（v0.17.44，PLAN 5.1）

本轮不新增 HTTP 端点，编排通过对话工具 `run_orchestration` / `create_orchestration`
进入，因此拒绝用例登记在工具级：

| # | 权限点 | 拒绝场景 | 用例（文件::函数） | 状态 |
|---|--------|----------|--------------------|------|
| O1 | `run_orchestration` 定义解析 | 定义不存在 / 跨用户个人定义不可见 → 不执行 | test_orchestration_definition_approval.py::test_user_cannot_write_global_definition_via_name_path（写侧）；test_orchestration_identity.py::test_personal_assets_are_not_borrowed_from_definition_author（个人资产隔离） | 有覆盖 |
| O2 | 子角色工具装配 | 低权限发起者越权包工具 → 装配层不绑定 | test_orchestration_identity.py::test_low_privilege_initiator_privileged_tool_denied | 有覆盖 |
| O3 | 子角色工具执行 | 直调越权包工具 → 包守卫拒绝且无数据 | 同 O2（装配层 + 直调层 + 无数据泄漏三重断言） | 有覆盖 |
| O4 | 发起者范围收窄 | 定义作者高权限、发起者低权限 → 检索范围按发起者收窄 | test_orchestration_identity.py::test_definition_author_privilege_does_not_expand_initiator_kb_scope | 有覆盖 |
| O5 | 危险工具 | 定义声明删除/审批/管理工具 → 校验拒绝；执行层兜底拒绝 | test_orchestration_definition.py::test_forbidden_tool_rejected（参数化全清单）；test_orchestration_boundaries.py::test_forbidden_tool_never_executes_even_if_injected | 有覆盖 |
| O6 | 沙箱边界 | 沙箱不可达 → 编排未启动；错误令牌 → 调用失败无数据；子进程环境剔除密钥 | test_orchestration_boundaries.py::test_sandbox_down_fails_closed_before_any_subagent / test_sandbox_wrong_token_returns_failure_without_data / test_sandbox_token_forwarded_on_orchestrated_tool_call / test_child_process_env_excludes_sandbox_secrets | 有覆盖 |
| O7 | 代际栅栏 | 会话删除/代际变更 → 下一步不执行、不写产出、不产生审批 | test_orchestration_boundaries.py::test_stale_generation_stops_next_step；test_orchestration_boundaries.py::test_running_orchestration_does_not_create_approvals | 有覆盖 |
| O8 | 全局定义创建 | user 角色不能直接写全局；approver/admin 全局创建必须审批；拒绝/异常不落盘 | test_orchestration_definition_approval.py::test_global_definition_requires_approval_then_persists / test_global_definition_rejected_does_not_persist / test_approval_unavailable_fails_closed / test_personal_definition_is_self_service_without_approval | 有覆盖 |

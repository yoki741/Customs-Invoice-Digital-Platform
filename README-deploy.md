# 部署到 DTD_nginx Portal（子路径 /customs-invoice/）

本平台不再独立对外监听端口，而是作为 Portal 的一个子路径应用接入：

```
浏览器 ──► Portal Nginx (DKT_kc_nginx, 443)
             ├── /oauth2/*         → oauth2-proxy（Keycloak 登录）
             ├── /api/auth/verify  → Portal Authorizer（校验会话、注入身份）
             └── /customs-invoice/ → 本容器 (DKT_customs-invoice:8000)
```

身份**只**由 Portal 侧注入，容器不信任客户端自带的任何身份头：

| Nginx 注入的请求头 | 含义 |
|--------------------|------|
| `Remote-Subject` | Keycloak `sub`（稳定主键） |
| `Remote-User` | 用户名 |
| `Remote-Display-Name` | 显示名 |
| `Remote-Groups` | Keycloak 组（逗号分隔，**无前导斜杠**：`admin,customs-invoice,customs-invoice-admin`；嵌套组只给叶子名） |
| `X-Forwarded-Prefix: /customs-invoice` | 子路径前缀 |

## 1. 运行模式（环境变量）

| 变量 | 生产取值 | 说明 |
|------|----------|------|
| `CUSTOMS_AUTH_MODE` | `sso` | `sso`：不提供口令登录，401 交给 Nginx 转 `/oauth2/start`；`local`：本地账号密码 + session（仅开发/直连 8000） |
| `CUSTOMS_SEED_ADMIN` | `0` | 是否在空库时创建 `admin/admin123`。生产必须为 `0`，管理员由 Keycloak 组 JIT 开通 |
| `APP_PREFIX` | `/customs-invoice` | 兜底前缀（正常情况下以 `X-Forwarded-Prefix` 为准） |
| `CUSTOMS_ADMIN_GROUPS` | `/customs-invoice-admin` | 兼容旧配置：这些组一律视为 `admin`（等价于 `CUSTOMS_ROLE_GROUPS` 里的 `:admin` 项）。**默认不含 Portal 全局 `/admin`** |
| `CUSTOMS_ROLE_GROUPS` | `/customs-invoice-admin:admin,/customs-invoice-type1:type1,/apps/customs-invoice:type2` | Keycloak 组 → 角色映射，格式 `组路径:角色` 逗号分隔；命中多个组时取最高权限（admin > type1 > type2）。比对时**同时接受** `/customs-invoice-admin`、`customs-invoice-admin`、嵌套组叶子名三种写法，因此 Keycloak 的 groups mapper 无论 `full.path` 为 true/false 都能命中。**默认不映射 Portal 全局 `/admin` 组** |
| `CUSTOMS_ROLE_SYNC` | `1` | `1`：每次请求把组映射结果同步进 `users.role`（改 Keycloak 组后刷新即生效）；`0`：只在首次开通时决定角色 |
| `CUSTOMS_DEFAULT_ROLE` | `type2` | 首次开通且未命中任何组映射时的角色 |
| `CUSTOMS_SSO_LOGIN_URL` | `/oauth2/start` | 未登录时的登录入口（Portal 根路径） |
| `CUSTOMS_PORTAL_INTERNAL_URL` | `http://DKT_kc_portal:8001` | 服务端拼装完整登出链（oauth2-proxy → Keycloak → 首页） |
| `CUSTOMS_OAUTH2_SIGNOUT_URL` | `/oauth2/sign_out` | Portal 不可用时的兜底登出地址 |

角色模型沿用本平台「账号管理」：`admin` / `type1` / `type2`，**由 Keycloak 组决定**：

| Keycloak 组 | `Remote-Groups` 实际值 | 角色 | 可见范围 |
|-------------|------------------------|------|----------|
| `/customs-invoice-admin` | `customs-invoice-admin` | `admin` | 全部功能 + `/admin` 账号管理（**本平台管理员唯一来源**） |
| `/customs-invoice-type1` | `customs-invoice-type1` | `type1` | 发票列表 + Excel 编辑 + 导出 |
| `/apps/customs-invoice`（Portal 基础访问权） | `customs-invoice` | `type2`（默认） | 仅查看每张发票的明细 |
| `/admin`（Portal 全局管理员） | `admin` | —（不映射） | 只能进门，本平台角色按默认 `type2`；要管理权限必须另加 `customs-invoice-admin` |

> 中间一列是现网 Keycloak 的 `groups` mapper 配置 `full.path=false` 时的真实取值：
> 顶层的 `/customs-invoice-admin` 变成 `customs-invoice-admin`，嵌套的
> `/apps/customs-invoice` 只保留叶子名 `customs-invoice`。`resolve_role_from_groups()`
> （`core/config.py`）会把配置里的组路径展开成「全路径 + 叶子名」候选键再比对，
> 所以上面几种写法都能命中；若以后把 mapper 改成 `full.path=true`，配置值不用改。
>
> **为什么不再映射 `/admin`**：Portal 超管自动获得报关平台的账号管理、导出、操作日志等
> 权限面太大。现在只有 `customs-invoice-admin` 组成员是本平台 `admin`；现网仅
> `portaladmin` 一人在该组内。确实需要让全部 Portal 超管进入管理后台时，把
> `CUSTOMS_ROLE_GROUPS` 补成 `"/admin:admin,<默认值>"` 即可（改环境变量，无需改代码）。

各角色进入平台后能看到的菜单（`webapp/templates/base.html` 按 `user.role` 渲染）：

| 角色 | 左侧菜单 | 可访问路由（后端守卫） |
|------|----------|------------------------|
| `type2` | 发票明细（`/details`）、报关发票（`/files`） | `require_type2`：明细 + 报关发票 |
| `type1` | 发票列表（`/invoices`）、装柜列表（`/shipments`）、数据导入（`/admin/upload`）、报关发票（`/files`） | `require_type1`：列表/装柜/导入/导出 + 报关发票 |
| `admin` | 以上全部 + 管理后台（`/admin`）、统计报表（`/admin/stats`）、账号管理、导出历史、操作日志 | `require_admin` + 上面两组 |

因此「让某个 Portal 账号能看到发票列表 / 汇总 / 导出」= 把他加进 `customs-invoice-type1`
（或 `customs-invoice-admin`）；只加 `apps/customs-invoice` 的账号按设计只看得到明细，
点 `/invoices` 会被 403（这一层是角色决定，与组名格式无关）。

规则：

1. 命中多个组时取最高权限（`admin` > `type1` > `type2`）。
2. 每次请求都会把命中结果同步进 `users.role`：在 Keycloak 里加/减组后**刷新页面即生效**，
   无需进本平台「账号管理」改角色（SSO 模式下该页角色为只读，并显示上面的映射表）。
   注意身份头来自 oauth2-proxy 的会话（`cookie_refresh = 4m`），所以改组后最迟约 4 分钟
   自动带到；想立刻看到效果就让用户「退出登录 → 重新登录」。
3. **未命中任何组时不改库内角色**（组名写错、`Remote-Groups` 为空都不会误降级）。
4. 首次登录（JIT）未命中任何组时用 `CUSTOMS_DEFAULT_ROLE`（默认 `type2`）。
5. 本平台「账号管理」的**停用**开关优先级最高：即使组里有 `admin` 也拒绝登录。
6. `/apps/customs-invoice` 是 Portal/Nginx 侧的**访问权**组（UMA 决策），
   与角色组相互独立：只给 `/customs-invoice-admin` 而不给 `/apps/customs-invoice`
   的用户仍会被 Portal 拦在门外，因此管理员的组应同时包含基础访问权组。

## 2. 构建与启动

```powershell
cd D:\DM\Customs-Invoice
docker build -t dkt-customs-invoice:latest .
```

Portal 侧 `docker/docker-compose.yml` 已包含 `customs-invoice` 服务（容器名
`DKT_customs-invoice`、`docker_dkt-net` 网络、卷 `customs_invoice_data:/app/data`、
健康检查 `/healthz`）。启动：

```powershell
cd D:\DM\DTD_nginx
docker compose -p dkt up -d --build customs-invoice
docker logs --tail 30 DKT_customs-invoice
```

首次启动会创建 SQLite 库 `data/app.db`（卷内），**请勿**重复 `down -v`。

## 3. Portal 侧注册

```powershell
docker exec -it DKT_kc_portal python manage.py shell
```

```python
from apps_registry.models import RegisteredApp
RegisteredApp.objects.update_or_create(
    slug="customs-invoice",
    defaults={
        "name": "报关单数字化平台",
        "internal_url": "http://DKT_customs-invoice:8000",
        "url": "/customs-invoice/",
        "is_active": True,
        "require_sso": True,
    },
)
```

随后生成并重载 Nginx 配置：

```powershell
docker exec DKT_kc_portal python manage.py generate_nginx_config --check
docker exec DKT_kc_portal python manage.py generate_nginx_config --reload
docker exec DKT_kc_nginx nginx -t
```

> 字段名以 `apps_registry/models.py` 实际定义为准，若模型已变更请按当前字段填写。

## 4. 授权访问

Keycloak 管理台 → Realm `ditu`（DITU Portal 现网；旧部署为 `dkt`）→ Groups：

1. `apps/customs-invoice`：**访问权**组，凡是要用本平台的用户都必须加入（Authorizer 的
   UMA 组策略只看这个组，未加入访问 `/customs-invoice/` 会被 403）。
2. `/customs-invoice-type1`：加入即 `type1`（列表 + 编辑 + 导出）。
3. `/customs-invoice-admin`：加入即 `admin`（含 `/admin` 账号管理）—— **本平台管理员的唯一来源**。
   Portal 全局管理员 `/admin` 组**不再**自动获得本平台管理员，只保证能进门。
4. 不进任何角色组、只在 `apps/customs-invoice` 的用户为 `type2`（仅看明细）。

新建这两个角色组时直接建在 **Groups 根层级**（名字即 `customs-invoice-type1` /
`customs-invoice-admin`），不要嵌入其他组之下——现网 mapper 是 `full.path=false`，
嵌套只会下发叶子名，容易与同名组混淆。

例：让 `zhangsan` 成为本平台管理员 —— 加入 `apps/customs-invoice` +
`customs-invoice-admin` 两个组（**只加 Portal 的 `admin` 组不够**），刷新页面即可进入
`/customs-invoice/admin`。**不需要**先以 `type2` 登录再手工改角色；改完组成员后角色由
身份头即时同步。

> 2026-10-26 起：本平台管理员**只由 `customs-invoice-admin` 组授予**，当前组内账号：
> `portaladmin`、`guchen`、`duyao`、`seamus`、`xialinqi`（= 曹汉杰 / `caohanjie@welsell.com.cn`，
> Keycloak 用户名与真实姓名不一致，按邮箱认人）。这 5 人同时也在 `apps/customs-invoice` 组内
> （`seamus` 原本没有该组，已补），可直接打开平台并进入 `/customs-invoice/admin`。
> 其余 Portal 超管仍能进门但角色是默认 `type2`，要扩容就照上面两步加组，不改代码。
>
> **收回管理员**：把账号移出 `customs-invoice-admin` 组，并确认他仍在
> `customs-invoice-type1`（→ `type1`）或 `apps/customs-invoice`（→ `type2`）组里，
> 下次请求即降级。三个角色组一个都不在时按「不降级」规则保留库内角色，
> 所以收权必须让他落到一个更低权限的组，不能只做「移出」。

角色映射可通过 `CUSTOMS_ROLE_GROUPS` 调整（见第 1 节），改容器环境变量后
`docker compose -p docker up -d customs-invoice` 生效，不需要重新构建镜像。

核对现网组（在 Keycloak 容器内用容器自带的引导管理员凭据，不打印密码）：

```bash
docker exec DKT_kc_keycloak sh -lc '
  K=/opt/keycloak/bin/kcadm.sh
  $K config credentials --server http://localhost:8080 --realm master \
    --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null
  $K get groups -r ditu --fields name,path'
```

`customs-invoice-type1` / `customs-invoice-admin` 已按上述要求建在根层级；给用户授权在
Keycloak 管理台 → Realm `ditu` → Users → 选用户 → Groups → Join Group 完成。

## 5. 验收清单

| 场景 | 期望 |
|------|------|
| 未登录浏览器访问 `/customs-invoice/` | 302 → `/oauth2/start?rd=%2Fcustoms-invoice%2F` |
| 已登录但不在 `apps/customs-invoice` 组 | 403 |
| 组成员访问 `/details` | 200，页面链接均为 `/customs-invoice/...` |
| 仅 `apps/customs-invoice` 组访问 `/admin` | 403（第二类账号无管理权限） |
| `customs-invoice-type1` 组访问 `/invoices` | 200，访问 `/admin` 为 403 |
| 组名不带斜杠（现网 `full.path=false`：`customs-invoice-admin` / `customs-invoice-type1` / `customs-invoice`） | 与带斜杠写法结果一致（`tests/test_role_groups.py` 第 9 组用例）；`admin` 这种 Portal 全局组则不解析出角色 |
| `customs-invoice-admin` 组成员访问 `/admin` | 200，账号管理页角色列只读 |
| 只有 Portal 全局 `admin` 组（无 `customs-invoice-admin`）访问 `/admin` | 403；`/details` 为 200（角色按默认 `type2`） |
| 刷新页面 / 深链收藏 | 不 404（前缀由 Nginx 剥离后转发） |
| 点「退出登录」 | 退出 Portal + Keycloak 会话，回到 Portal 首页 |
| 直接访问容器 8000（容器间伪造 `Remote-User`） | 401/403，不会获得身份 |
| `docker restart DKT_customs-invoice` | 数据仍在（`data/app.db` 在卷内） |

## 6. 排错

| 现象 | 排查 |
|------|------|
| 页面样式/脚本 404 | Nginx 未注入 `X-Forwarded-Prefix`；检查生成的 `apps/customs-invoice.conf` |
| 登录后无限跳转 | `error_page 401 = /oauth2/start` 只应配在 HTML location，API 走 401 JSON |
| 全部 403 | Authorizer 未放行该应用（`RegisteredApp.is_active` / 组授权） |
| 登录用户串号 | 确认仅在 Nginx 层注入 `Remote-*`，客户端自带的同名头必须被 `proxy_set_header` 覆盖 |
| 导出/上传失败 | 检查卷剩余空间与 `data/outputs` 权限（容器以 uid 10001 运行） |
| 角色改不动 / 被改回去 | SSO 模式下角色由 Keycloak 组决定（`CUSTOMS_ROLE_GROUPS`），请改组而不是改库；`docker logs DKT_customs-invoice \| grep role.sync` 可看到每次同步 |
| 加组后仍是旧角色 | 先看 `docker logs DKT_customs-invoice \| grep -F "组"` 里 JIT/同步日志打印的组名：现网 `full.path=false` 下发的是无斜杠值（`customs-invoice-admin`、`customs-invoice-type1`、嵌套组只给叶子名）。本平台已兼容两种写法；若日志显示 `来源 默认`，说明组名与 `CUSTOMS_ROLE_GROUPS` 完全对不上 |
| Portal 超管登录后只有发票明细、进不了 `/admin` | 设计如此：Portal 全局 `admin` 组不再映射为本平台管理员，把该账号加入 `customs-invoice-admin` 组即可（当前仅 `portaladmin` 在该组） |

## 7. 本地开发（不接 Portal）

```powershell
set CUSTOMS_AUTH_MODE=local
python -m uvicorn webapp.main:app --host 127.0.0.1 --port 8000
```

本地模式沿用 `admin/admin123`（`CUSTOMS_SEED_ADMIN` 默认 1）与 session 登录，
不注入 `Remote-*` 也能正常使用。

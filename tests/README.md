# 测试数据库

纯单元测试默认直接运行。需要数据库的测试只接受显式配置的 MySQL/MariaDB 测试环境，
不会读取应用 `.env` 中的业务数据库地址。

任选一种方式配置：

```powershell
# 测试管理员账号：pytest 创建 quantdesk_test_* 临时库，结束后自动删除。
$env:QUANTDESK_TEST_DATABASE_ADMIN_URL = 'mysql+pymysql://test_admin:password@127.0.0.1:3306/mysql'

# 或使用已经预配的专用库；库名必须以 quantdesk_test_ 开头，pytest 不删除该库。
$env:QUANTDESK_TEST_DATABASE_URL = 'mysql+pymysql://test_user:password@127.0.0.1:3306/quantdesk_test_ci'

pytest
```

需要 TLS 时另外设置 `QUANTDESK_TEST_DB_SSL_REQUIRED`、
`QUANTDESK_TEST_DB_SSL_VERIFY_IDENTITY` 和 `QUANTDESK_TEST_DB_SSL_CA`。
每个数据库测试前后只会清理已通过前缀与当前库双重校验的专用测试库。

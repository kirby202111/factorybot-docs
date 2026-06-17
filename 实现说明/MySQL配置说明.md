# MySQL 配置说明

> 适用范围：本文是项目初始化后的 MySQL 与 Spring MyBatis 统一配置说明，仅描述项目级基础设施配置，不包含具体业务表、业务事件、Outbox、消费幂等等业务实现细节。
>
> 业务事件相关的 MySQL 表结构、Outbox 领取 SQL、消费幂等表等内容，见《[业务事件/MySQL配置说明](业务事件/MySQL配置说明.md)》。

---

## 1. 版本与基础约定

建议使用：

```text
MySQL 8.0+
InnoDB 存储引擎
utf8mb4 字符集
Spring Boot + MyBatis
HikariCP 连接池
```

基础约定：

- 生产环境不使用应用账号执行 DDL。
- 生产环境不使用 `root` 账号运行应用。
- 所有服务使用独立数据库账号。
- 表结构变更通过 Flyway / Liquibase 或受控 SQL 脚本管理。
- 应用事务中不要调用外部 HTTP、Kafka、文件服务等外部 IO。

---

## 2. MySQL 服务器配置

### 2.1 推荐 `my.cnf` / `mysqld.cnf`

以下参数作为项目初始化基线，实际值需要结合服务器内存、CPU、连接数和部署方式调整。

```ini
[mysqld]
# 基础
port = 3306
bind-address = 0.0.0.0
default_storage_engine = InnoDB

# 字符集
character-set-server = utf8mb4
collation-server = utf8mb4_0900_ai_ci

# 时区
# 如果服务器统一使用东八区，可以设置为 +08:00；如果统一使用 UTC，则应用侧也必须保持一致。
default-time-zone = '+08:00'

# SQL 模式
sql_mode = STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION

# 连接
max_connections = 500
max_allowed_packet = 64M
wait_timeout = 28800
interactive_timeout = 28800

# InnoDB
innodb_flush_log_at_trx_commit = 1
innodb_buffer_pool_size = 2G
innodb_buffer_pool_instances = 2
innodb_log_buffer_size = 64M

# MySQL 8.0.30+ 推荐使用 innodb_redo_log_capacity；低版本可使用 innodb_log_file_size。
innodb_redo_log_capacity = 1G

# 临时表
tmp_table_size = 64M
max_heap_table_size = 64M

# 慢查询
slow_query_log = ON
slow_query_log_file = /var/log/mysql/slow.log
long_query_time = 1
log_queries_not_using_indexes = OFF

# Binlog。需要主从复制、审计或 CDC 时开启。
server-id = 1
log_bin = mysql-bin
binlog_format = ROW
binlog_row_image = FULL
expire_logs_days = 7
```

### 2.2 参数说明

| 参数 | 建议 | 说明 |
|---|---:|---|
| `character-set-server` | `utf8mb4` | 支持完整 Unicode 字符。 |
| `collation-server` | `utf8mb4_0900_ai_ci` | MySQL 8.0 默认推荐排序规则。 |
| `default-time-zone` | `+08:00` 或统一 UTC | 必须与应用侧时间策略一致。 |
| `sql_mode` | 严格模式 | 避免脏数据被静默截断或隐式转换。 |
| `max_connections` | 按服务数量调整 | 需要结合各服务连接池上限统一核算。 |
| `max_allowed_packet` | `64M` 起步 | 避免较大 SQL、批量写入或 JSON 字段写入失败。 |
| `innodb_flush_log_at_trx_commit` | `1` | 每次事务提交刷盘，可靠性优先。 |
| `innodb_buffer_pool_size` | 物理内存 50%~70% | 专用数据库服务器可适当提高。 |
| `binlog_format` | `ROW` | 复制、审计、CDC 场景更可靠。 |
| `long_query_time` | `1` | 生产环境建议开启慢查询定位问题。 |

---

## 3. 数据库与账号初始化

示例：

```sql
CREATE DATABASE mes_service
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

CREATE USER 'mes_service_app'@'%' IDENTIFIED BY 'change_me';

GRANT SELECT, INSERT, UPDATE, DELETE
ON mes_service.*
TO 'mes_service_app'@'%';

FLUSH PRIVILEGES;
```

说明：

- `mes_service_app` 是应用运行账号，只授予 DML 权限。
- DDL 迁移使用单独账号，例如 `mes_service_migrator`。
- 不同服务使用不同库或至少不同账号，避免权限扩大。
- 如果使用 Flyway / Liquibase，迁移账号可以具备建表、改表、建索引权限，应用账号不具备。

---

## 4. Spring 数据源配置

Spring Boot 配置示例：

```yaml
spring:
  datasource:
    url: jdbc:mysql://${MYSQL_HOST:localhost}:${MYSQL_PORT:3306}/${MYSQL_DATABASE:mes_service}?useUnicode=true&characterEncoding=utf8&useSSL=false&serverTimezone=Asia/Shanghai&allowPublicKeyRetrieval=true&rewriteBatchedStatements=true
    username: ${MYSQL_USERNAME:mes_service_app}
    password: ${MYSQL_PASSWORD:change_me}
    driver-class-name: com.mysql.cj.jdbc.Driver
    hikari:
      pool-name: mes-service-hikari
      maximum-pool-size: 20
      minimum-idle: 5
      connection-timeout: 30000
      validation-timeout: 5000
      idle-timeout: 600000
      max-lifetime: 1800000
      keepalive-time: 300000
```

关键参数：

| 配置 | 建议 | 说明 |
|---|---:|---|
| `serverTimezone` | `Asia/Shanghai` | 与 MySQL 服务端时区策略保持一致。 |
| `rewriteBatchedStatements` | `true` | 提升 MySQL 批量插入、批量更新效率。 |
| `maximum-pool-size` | `20` 起步 | 按服务实例数、接口并发、后台任务数量统一核算。 |
| `minimum-idle` | `5` | 保留少量空闲连接，避免突发请求建连。 |
| `connection-timeout` | `30000` | 获取连接超时时间。 |
| `max-lifetime` | 小于数据库连接回收时间 | 避免连接被数据库或网络设备提前断开。 |
| `keepalive-time` | `300000` | 保持连接活性，减少空闲连接被中间设备断开。 |

连接数核算示例：

```text
总连接上限 ≈ 服务实例数 × 每实例 maximum-pool-size + 运维/迁移/报表预留连接
```

不要让所有服务连接池上限之和超过 MySQL `max_connections`。

---

## 5. MyBatis 配置

### 5.1 Maven 依赖

```xml
<dependency>
    <groupId>com.mysql</groupId>
    <artifactId>mysql-connector-j</artifactId>
</dependency>

<dependency>
    <groupId>org.mybatis.spring.boot</groupId>
    <artifactId>mybatis-spring-boot-starter</artifactId>
</dependency>
```

如果项目统一使用 MyBatis-Plus，可替换为 MyBatis-Plus 的 Spring Boot Starter，但基础数据源、事务和 MySQL 服务端配置仍遵循本文。

### 5.2 Spring Boot 配置

```yaml
mybatis:
  mapper-locations: classpath*:mapper/**/*.xml
  type-aliases-package: com.company.mes.**.domain,com.company.mes.**.infrastructure.persistence
  configuration:
    map-underscore-to-camel-case: true
    cache-enabled: false
    local-cache-scope: statement
    jdbc-type-for-null: 'NULL'
    default-fetch-size: 100
    default-statement-timeout: 30
```

说明：

| 配置 | 建议 | 说明 |
|---|---:|---|
| `mapper-locations` | `classpath*:mapper/**/*.xml` | 统一放置 MyBatis XML。 |
| `map-underscore-to-camel-case` | `true` | 数据库下划线字段自动映射 Java 驼峰属性。 |
| `cache-enabled` | `false` | 默认关闭二级缓存，避免缓存一致性问题。 |
| `local-cache-scope` | `statement` | 减少一级缓存导致的同事务内陈旧读取误判。 |
| `jdbc-type-for-null` | `NULL` | 避免部分驱动处理 null 参数异常。 |
| `default-fetch-size` | `100` | 控制默认拉取大小。 |
| `default-statement-timeout` | `30` | 默认 SQL 超时时间，单位秒。 |

### 5.3 Mapper 扫描

```java
@SpringBootApplication
@MapperScan("com.company.mes.**.infrastructure.persistence.mapper")
public class MesServiceApplication {
}
```

约定：

```text
src/main/java/com/company/mes/<module>/infrastructure/persistence/mapper/
src/main/resources/mapper/<module>/
```

Mapper 接口和 XML 文件应按模块分目录，避免所有 SQL 混在同一层级。

---

## 6. 事务配置

启用事务：

```java
@EnableTransactionManagement
@SpringBootApplication
public class MesServiceApplication {
}
```

应用服务方法使用：

```java
@Transactional(rollbackFor = Exception.class)
public void handleCommand(Command command) {
    // 修改本地数据库状态
}
```

建议：

- 事务边界放在 application service，不放在 controller。
- 单个事务只处理本地数据库状态变更。
- 不在事务内调用外部 HTTP、Kafka、文件服务、RPC。
- 批处理任务分批提交，避免超长事务。

---

## 7. 事务隔离级别

MySQL 默认隔离级别是：

```text
REPEATABLE READ
```

项目默认可以使用 MySQL 默认隔离级别。如果服务存在大量任务领取、批量扫描、补偿处理，并希望减少间隙锁影响，可在应用侧设置：

```yaml
spring:
  datasource:
    hikari:
      transaction-isolation: TRANSACTION_READ_COMMITTED
```

要求：

- 隔离级别变更必须按服务评估，不做全项目无差别修改。
- 对并发一致性有严格要求的写操作，仍应使用唯一约束、版本号或显式锁保证。

---

## 8. SQL 编写约定

### 8.1 命名

- 表名使用小写下划线。
- 字段名使用小写下划线。
- 主键名默认 `id`。
- 创建时间、更新时间统一使用 `created_at`、`updated_at`。
- 逻辑删除时间可使用 `deleted_at`。

### 8.2 查询

- 禁止生产代码中无条件全表扫描大表。
- 分页查询必须有稳定排序字段。
- 高并发接口只查询必要字段，避免 `SELECT *`。
- 大字段、JSON 字段不参与高频列表查询。

### 8.3 写入

- 批量写入开启 `rewriteBatchedStatements=true`。
- 大批量导入分批提交。
- 需要幂等的写入应依赖唯一约束，不只依赖应用层判断。

---

## 9. 索引与慢查询

基础原则：

- 高频等值查询字段应建立索引。
- 常见组合查询按“等值条件在前，范围和排序字段在后”设计组合索引。
- 不为低选择性字段单独建过多索引，例如只给 `status` 单列建索引通常收益有限。
- 大字段 `TEXT`、`JSON` 不直接参与高频条件查询。
- 每个索引都会增加写入成本，不能无节制增加索引。

慢查询要求：

- 生产环境开启慢查询日志。
- 默认 `long_query_time=1` 秒起步。
- 慢 SQL 必须结合 `EXPLAIN`、索引、数据量和调用频率分析。

---

## 10. JSON 字段使用规范

MySQL 8.0 支持 `JSON` 字段，但项目中应克制使用。

适合使用 JSON 的场景：

- 扩展属性。
- 外部系统原始报文摘要。
- 审计快照。
- 不参与核心查询条件的结构化附加信息。

不适合使用 JSON 的场景：

- 高频查询条件。
- 需要强约束的核心字段。
- 需要复杂统计聚合的字段。

建议：

- 核心状态、编码、数量、时间、责任人等字段必须结构化为普通列。
- JSON 中需要高频查询的字段，应冗余为普通列并建立索引。
- 如果 ORM 映射成本高，可以使用 `TEXT` 保存 JSON 字符串。

---

## 11. 迁移脚本配置

如果使用 Flyway：

```yaml
spring:
  flyway:
    enabled: true
    locations: classpath:db/migration
    baseline-on-migrate: true
```

脚本命名建议：

```text
V202606170001__init_schema.sql
V202606170002__add_xxx_index.sql
```

要求：

- 生产环境不使用 `ddl-auto: update`。
- DDL 脚本需要可审查、可追溯。
- 大表加字段、加索引、改字段类型要评估锁表和执行时间。
- 删除字段、删除索引前先确认无代码、报表、集成任务依赖。

---

## 12. 环境变量建议

```text
MYSQL_HOST
MYSQL_PORT
MYSQL_DATABASE
MYSQL_USERNAME
MYSQL_PASSWORD
```

要求：

- 密码不写入代码仓库。
- 生产环境通过配置中心、密钥系统或部署平台注入。
- 本地开发可使用 `.env` 或本地配置文件，但不要提交真实密码。

---

## 13. 初始化检查清单

### 13.1 MySQL 服务器

- [ ] MySQL 使用 8.0+。
- [ ] 默认存储引擎为 InnoDB。
- [ ] 默认字符集为 `utf8mb4`。
- [ ] 默认排序规则为 `utf8mb4_0900_ai_ci`。
- [ ] 已确认服务端时区策略。
- [ ] 已开启严格 SQL 模式。
- [ ] 已开启慢查询日志。
- [ ] 已按服务器规格调整 `innodb_buffer_pool_size`。
- [ ] 已按部署规模核算 `max_connections`。
- [ ] 需要复制、审计或 CDC 时已开启 ROW binlog。

### 13.2 Spring MyBatis

- [ ] 已配置 MySQL Connector/J。
- [ ] 已配置 MyBatis Starter。
- [ ] 已配置 HikariCP 连接池。
- [ ] 已配置 `mapper-locations`。
- [ ] 已启用 `map-underscore-to-camel-case`。
- [ ] 已关闭 MyBatis 二级缓存。
- [ ] 已配置默认 SQL 超时时间。
- [ ] 已启用 Spring 事务管理。
- [ ] 生产环境表结构由迁移脚本管理。

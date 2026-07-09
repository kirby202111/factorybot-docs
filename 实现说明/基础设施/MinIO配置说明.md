# MinIO 配置说明

> 适用范围：本文是项目级对象存储（MinIO）的基础设施配置说明，仅描述 MinIO 部署形态、Bucket 规划、访问凭证、上传/下载与预签名、生命周期、CORS、监控等基础设施配置，不包含具体业务（AOI 图像缩略图生成、缺陷框叠加、死信回放业务逻辑等）实现细节。
>
> 与领域模型的衔接：大文件/曲线/图像走对象存储、主流报文只承载 URI + `sha256`，是设备数据接入上下文 INV-15 的实现落地，见《[领域模型/设备管理服务/领域建模/设备数据接入上下文](../../领域模型/设备管理服务/领域建模/设备数据接入上下文.md)》§1.4 / §6.1.4。
>
> 与 Kafka 的衔接：Kafka 消息只传引用和元数据，大文件本体落 MinIO，见《[基础设施/Kafka配置说明](Kafka配置说明.md)》§3。

---

## 1. 部署形态

MinIO 是 S3 兼容的对象存储，单二进制、零依赖。按规模分三种形态：

| 形态 | 启动方式 | 容错 | 适用 |
|---|---|---|---|
| 单节点单机 | `minio server /data` | 无冗余，磁盘坏即丢 | 本地开发 |
| 单节点纠删码 | `minio server /data{1...4}` | 挂半数盘仍可读 | **车间级生产基线** |
| 分布式 | `minio server http://minio{1...4}/data{1...4}` | 节点级容错 | 跨车间多节点 |

说明：

- 本项目车间级部署推荐**单节点纠删码**（4 块盘起步），默认 erasure code，挂一半盘仍可读，部署成本与单机相当。
- 分布式形态需要规划节点拓扑、域名、证书，跨车间规模才需要。
- 生产环境不建议用单节点单机形态。

---

## 2. MinIO Server 启动配置

### 2.1 启动命令

```bash
# 单节点纠删码（车间级基线）
minio server /data{1...4} --console-address ":9001"
```

### 2.2 环境变量

```bash
# Root 凭证（仅运维与初始化使用，业务不直接用 root）
MINIO_ROOT_USER=minio-admin
MINIO_ROOT_PASSWORD=<strong-password>

# Web Console
MINIO_BROWSER=on

# API 与 Console 端口
# 9000 = S3 API
# 9001 = Web Console
```

关键参数：

| 参数 | 建议 | 说明 |
|---|---|---|
| `MINIO_ROOT_USER` | 3~8 位 | Root 用户名，仅用于初始化与运维。 |
| `MINIO_ROOT_PASSWORD` | 强密码 | Root 密码，生产环境必须强随机。 |
| `MINIO_BROWSER` | `on` | 开启 Web Console，便于运维浏览对象。 |
| API 端口 | `9000` | S3 兼容 API，业务与 SDK 连此端口。 |
| Console 端口 | `9001` | 运维 Web 控制台，不应暴露到公网。 |

注意：

- Root 凭证仅用于初始化 bucket、创建业务专用 access key、运维排查。
- 业务服务用专用 access key（见 §4），不使用 root 凭证。

---

## 3. Bucket 规划

对象 Key 命名归业务侧，但 Bucket 与前缀分区需要在此定基线，以便生命周期规则按前缀自动清理。

### 3.1 命名规范

```text
dc/{kind}/{equipment_id}/{yyyy-MM-dd}/{msg_id}.{ext}
```

- `kind`：数据类型前缀，决定生命周期。
- `equipment_id`：来源设备。
- `yyyy-MM-dd`：按天分桶，便于按天做生命周期与排查。
- `msg_id`：报文全局唯一标识，与主流 DataPacket 的 `msg_id` 对齐。

### 3.2 前缀与保留期

| 前缀 | 数据类型 | 保留期 | 对应领域模型 |
|---|---|---|---|
| `deadletter/` | 死信原始字节 | 7 天滚动 | INV-07 |
| `aoi-image/` | AOI 缺陷图像 | 按配置（建议 30 天） | §5.6.2 / `ImageReference` |
| `log/` | 烧录日志 | 按配置（建议 30 天） | §5.6.5 / `LogArtifact` |
| `curve/` | 扭矩曲线等高密度曲线 | 随报文生命周期 | §5.6.7 / `TorqueMeasurement` |

注意：

- 保留期与领域模型的 `retain_until` 字段对齐，对象存储侧用生命周期规则到期删除。
- 引用未删除前，对象不应先被删；生命周期天数应大于等于业务 `retain_until` 的最大值。

---

## 4. 访问凭证与 IAM

### 4.1 业务专用 Access Key

通过 `mc` 或 Console 创建业务专用 access key，仅授予目标 bucket 的读写权限，不使用 root 凭证。

```bash
mc admin user svcacct add minio-admin \
  --access-key factorybot-app \
  --secret-key <app-secret-key>
```

### 4.2 Bucket Policy

为业务账号绑定最小权限策略：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::factorybot-artifacts", "arn:aws:s3:::factorybot-artifacts/*"]
    }
  ]
}
```

要求：

- 业务账号只授予实际需要的 bucket 权限，不授予 `s3:*` 全权限。
- 预签名 URL 的签发仍需校验业务用户能否访问该设备数据，权限检查在签发环节完成（见 §7）。

---

## 5. Spring 集成（连接配置）

MinIO 是 S3 兼容存储，推荐使用 AWS S3 SDK v2，保留未来切换 OSS / Ceph / RDS 的能力，避免厂商锁定。MinIO 官方 Java SDK 作为轻量备选。

### 5.1 Maven 依赖

```xml
<dependency>
    <groupId>software.amazon.awssdk</groupId>
    <artifactId>s3</artifactId>
</dependency>
```

AWS SDK v2 不在 Spring Boot BOM 内，需显式声明版本，按实际可用版本对齐。

### 5.2 连接配置

```yaml
app:
  object-storage:
    endpoint: ${OBJECT_STORAGE_ENDPOINT:http://minio:9000}
    region: ${OBJECT_STORAGE_REGION:us-east-1}
    access-key: ${OBJECT_STORAGE_ACCESS_KEY}
    secret-key: ${OBJECT_STORAGE_SECRET_KEY}
    path-style-access: true
    bucket: ${OBJECT_STORAGE_BUCKET:factorybot-artifacts}
    presign-expiry: 15m
```

环境变量：

```text
OBJECT_STORAGE_ENDPOINT
OBJECT_STORAGE_ACCESS_KEY
OBJECT_STORAGE_SECRET_KEY
OBJECT_STORAGE_BUCKET
```

关键参数：

| 配置 | 推荐值 | 说明 |
|---|---|---|
| `endpoint` | `http://minio:9000` | MinIO S3 API 地址。 |
| `region` | `us-east-1` | MinIO 默认 region，任意值即可，但必须配置。 |
| `path-style-access` | `true` | **MinIO 必须开启**，否则默认 virtual-host style 会解析失败。 |
| `bucket` | `factorybot-artifacts` | 业务 bucket。 |
| `presign-expiry` | `15m` | 预签名 URL 默认有效期，内网可放宽到 60m。 |

### 5.3 S3Client / S3Presigner Bean

```java
@Configuration
public class S3Config {

    @Bean
    public S3Client s3Client(ObjectStorageProperties props) {
        return S3Client.builder()
            .endpointOverride(URI.create(props.getEndpoint()))
            .region(Region.of(props.getRegion()))
            .credentialsProvider(StaticCredentialsProvider.create(
                AwsBasicCredentials.create(props.getAccessKey(), props.getSecretKey())))
            .serviceConfiguration(b -> b.pathStyleAccessEnabled(true))
            .build();
    }

    @Bean
    public S3Presigner s3Presigner(ObjectStorageProperties props) {
        return S3Presigner.builder()
            .endpointOverride(URI.create(props.getEndpoint()))
            .region(Region.of(props.getRegion()))
            .credentialsProvider(StaticCredentialsProvider.create(
                AwsBasicCredentials.create(props.getAccessKey(), props.getSecretKey())))
            .serviceConfiguration(b -> b.pathStyleAccessEnabled(true))
            .build();
    }
}
```

注意：

- `pathStyleAccessEnabled(true)` 是 MinIO 接入的关键，漏配会导致请求 403 / 域名解析失败。
- `S3Client` 用于服务端直接上传/下载，`S3Presigner` 用于生成预签名 URL。

---

## 6. 上传配置

大文件上传推荐**预签名 PUT 网关直传**：平台签发预签名 URL，网关直传 MinIO，平台不经流量。

### 6.1 预签名 PUT

```java
PresignedPutObjectRequest presigned = s3Presigner.presignPutObject(p -> p
    .signatureDuration(Duration.ofMinutes(15))
    .putObjectRequest(r -> r
        .bucket(bucket)
        .key(key)
        .contentType("image/png")));
URL uploadUrl = presigned.url();
```

### 6.2 Content-Type 必须设对

上传时必须设置正确的 `Content-Type`，否则浏览器会触发下载而不是内联渲染：

| 数据类型 | Content-Type |
|---|---|
| AOI 图像 | `image/png` / `image/jpeg` |
| 烧录日志（文本） | `text/plain` |
| 扭矩曲线（JSON） | `application/json` |
| 死信原始字节 | `application/octet-stream` |

### 6.3 大文件分片续传

烧录日志等大文件断网重传必须走 multipart，避免整文件重传：

- 流程：`CreateMultipartUpload` -> 多个 `UploadPart`（可并行、可断点续传） -> `CompleteMultipartUpload`。
- 断网恢复后，已上传的 part 不重传，只补未完成的 part。
- 网关侧可用 STS 临时凭证 + S3 SDK 完整 multipart，或由平台逐 part 签发预签名 URL。

### 6.4 完整性校验

- 客户端上传前计算 `sha256`，与主流 DataPacket 的 `sha256` 对齐（INV-15）。
- SDK 上传时通过 `x-amz-content-sha256` 做服务端校验。
- 上传成功、校验通过后，平台才生成 `object_uri` 并 `DataPacket.seal`，保证引用不先于对象存在。

---

## 7. 下载与查看配置

### 7.1 预签名 GET

```java
PresignedGetObjectRequest presigned = s3Presigner.presignGetObject(g -> g
    .signatureDuration(Duration.ofMinutes(15))
    .getObjectRequest(r -> r.bucket(bucket).key(key)));
URL viewUrl = presigned.url();
```

说明：

- 前端拿到预签名 URL 后，`<img src="...">` 直接渲染（图片）或新窗口打开（日志）。
- 浏览器直连 MinIO 拉取，流量不经过业务服务器。

### 7.2 权限与审计

- 预签名 URL = 持有即可访问的凭证，权限检查放在**签发环节**：后端签发前校验该用户能否访问该设备数据。
- 签发时记录审计日志（用户、对象 Key、时间）。
- 预签名 URL 不应被截屏外传，它本身即钥匙。

### 7.3 CORS

`<img>` 标签跨域加载不受 CORS 限制。**但如果前端用 canvas 读取像素**（如导出带缺陷框的图、图像比对），必须在 MinIO 配置 CORS：

```json
[
  {
    "AllowedOrigins": ["https://mes.example.com"],
    "AllowedMethods": ["GET", "PUT", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

```bash
# 通过 mc 设置 CORS
mc cors set myminio/factorybot-artifacts cors.json
```

### 7.4 Range 请求（日志尾部查看）

烧录日志可能几十 MB，操作员通常只看尾部报错。S3 协议支持 Range 请求，只取尾部几 KB：

```text
Range: bytes=-8192
```

预签名 URL 同样支持 Range，前端可在 fetch 时携带 Range header 拉取尾部。

### 7.5 对象过期降级

`retain_until` 到期后，MinIO 生命周期规则删除对象，此时预签名 URL 返回 404。业务侧必须处理"已过保留期"的降级提示，不让前端裸遇 404。

---

## 8. 预览访问模式

预览按数据类型分流为两类场景，MinIO 在两边都只负责"吐字节"，区别在中间有无业务代理层。通用预签名 GET / CORS / Range 基础见 §7，本节给出两种访问模式的 MinIO 用法与选型。

| 场景 | 方案 | 典型数据 |
|---|---|---|
| 强权限、生效版控制、水印审计 | A 后端代理流式 | SOP / 工艺图纸 / 质检报告（PDF） |
| 内网、高频、大文件 | B 预签名直读 | AOI 图像 / 烧录日志 |

### 8.1 方案 A：后端代理流式预览（SOP / 工艺文档）

后端用 `S3Client.getObject()` 流式取对象，边读边写响应。权限校验、生效版解析、审计都在调 MinIO 之前完成（业务层职责），MinIO 只负责吐字节。

```java
@GetMapping("/sop/{docId}/preview")
public void preview(@PathVariable String docId, HttpServletResponse response)
        throws IOException {
    // 1. 查文档主数据（MySQL），解析当前生效版的 object_key + version_id（业务层职责）
    SopVersion active = sopService.resolveActiveVersion(docId);
    // 2. 权限校验 + 审计（业务层职责，略）

    // 3. 从 MinIO 流式取 PDF，显式指定生效版本
    GetObjectRequest req = GetObjectRequest.builder()
        .bucket(bucket)
        .key(active.getObjectKey())
        .versionId(active.getVersionId())
        .build();
    try (ResponseInputStream<GetObjectResponse> in = s3Client.getObject(req)) {
        GetObjectResponse meta = in.response();
        response.setContentType("application/pdf");
        response.setHeader("Content-Disposition", "inline");
        response.setContentLengthLong(meta.contentLength());
        in.transferTo(response.getOutputStream());
    }
}
```

关键点：

- `versionId` 显式取生效版，不取 `latest`（latest 可能是草稿），见 §8.3。
- `Content-Disposition: inline` 让浏览器原生 PDF viewer 预览，不触发下载。
- `transferTo` 流式转发，几十 MB 图纸也不进内存。
- 预签名 URL 过期问题在此方案不存在（URL 不下发前端）。

### 8.2 方案 B：预签名直读预览（设备大文件）

后端用 `S3Presigner` 生成预签名 GET URL（生成方式见 §7.1），前端直接加载，浏览器直连 MinIO。

前端：

```html
<!-- 图片 -->
<img src="<presigned-url>">
<!-- PDF -->
<iframe src="<presigned-url>" width="100%" height="800"></iframe>
```

关键点：

- 预签名 URL 有过期，`<iframe>` 长期嵌入需前端定时刷新（SOP 长开屏不建议用此方案）。
- 大 PDF 的 Range 优势：浏览器原生 PDF viewer 发 `Range` 分段加载，直读时浏览器直接对 MinIO 发 Range，MinIO 原生支持 206，首页秒开。方案 A 代理要自己处理 Range / 206，较复杂。

### 8.3 配合对象版本控制（SOP 版本生效）

SOP 会更新，需保留历史版并指定生效版。MinIO bucket versioning 提供对象级版本：

```bash
mc version enable myminio/factorybot-docs
```

开启后同 key 多次上传产生不同 `version_id`。**版本生效语义（哪份是当前生效版、审批、回滚）归业务主数据（MySQL），不在 MinIO**；MinIO 只存文件本体 + `version_id`。业务主数据样例：

```text
doc_id | version_no | status     | object_key       | version_id | sha256 | effective_from | effective_to
SOP-01 | 1.0        | SUPERSEDED | sop/SOP-01.pdf   | v1...      | ...    | 2026-01-01     | 2026-06-30
SOP-01 | 2.0        | EFFECTIVE  | sop/SOP-01.pdf   | v2...      | ...    | 2026-07-01     | null
```

预览时 `resolveActiveVersion(docId)` 查 `status=EFFECTIVE`（或当前时间落在 effective 区间）拿到 `version_id`，再 `getObject` 显式取该版。工位永远看生效版，历史版可追溯、可回滚，不会被误预览。

### 8.4 选型小结

| 维度 | 方案 A 代理流式 | 方案 B 预签名直读 |
|---|---|---|
| 适用 | SOP / 工艺文档 | AOI 图 / 烧录日志 |
| 权限/生效版/审计 | 代理层统一做 | 签发环节做 |
| 流量 | 过业务服务器 | 不过业务服务器 |
| 大文件分段 | 需自己处理 Range/206 | 浏览器原生 Range |
| URL 过期 | 无 | 有，需刷新 |
| MinIO 用法 | `getObject(versionId)` + inline | `presignGetObject` |

两类不是二选一，按数据类型分流：SOP 走 A，设备大文件走 B。

---

## 9. 生命周期规则

按 §3.2 的前缀与保留期配置 bucket 生命周期，到期自动删除，对齐领域模型的 `retain_until`。

```json
{
  "Rules": [
    {
      "ID": "deadletter-7d",
      "Status": "Enabled",
      "Filter": { "Prefix": "deadletter/" },
      "Expiration": { "Days": 7 }
    },
    {
      "ID": "aoi-image-30d",
      "Status": "Enabled",
      "Filter": { "Prefix": "aoi-image/" },
      "Expiration": { "Days": 30 }
    },
    {
      "ID": "log-30d",
      "Status": "Enabled",
      "Filter": { "Prefix": "log/" },
      "Expiration": { "Days": 30 }
    }
  ]
}
```

```bash
# 通过 mc 导入生命周期规则
mc ilm import myminio/factorybot-artifacts lifecycle.json
```

要求：

- 生命周期天数与业务 `retain_until` 对齐，不小于其最大值。
- 规则按前缀分区，不同数据类型差异化保留。
- 规则变更后需验证到期对象是否如期清理。

---

## 10. 监控与运维基线

需要关注：

| 指标 | 说明 |
|---|---|
| MinIO 节点存活 | 节点是否在线。 |
| 磁盘状态 | 各盘在线/离线，erasure code 降级状态。 |
| bucket 容量 | 各前缀占用与增长趋势。 |
| 对象数量 | 总对象数，警惕百万级小文件影响性能。 |
| 上传失败率 | 预签名 PUT / multipart 上传失败率。 |
| 4xx 错误率 | 预签名过期、对象不存在（404）等。 |
| 生命周期清理 | 到期对象清理是否如期执行。 |
| 磁盘使用率 | 数据盘使用率。 |

告警建议：

- MinIO 节点下线。
- erasure code 降级（可用盘数低于阈值）。
- 磁盘使用率超过阈值。
- 上传失败率持续升高。
- 生命周期规则未按期清理对象。

---

## 11. 初始化检查清单

### 11.1 MinIO Server

- [ ] 生产环境采用单节点纠删码（4 盘起）或分布式形态。
- [ ] `MINIO_ROOT_PASSWORD` 为强随机密码。
- [ ] API 端口（9000）与 Console 端口（9001）已规划，Console 不暴露公网。
- [ ] 数据盘容量已规划，按数据类型保留期估算。

### 11.2 Bucket 与权限

- [ ] 业务 bucket 已显式创建（如 `factorybot-artifacts`）。
- [ ] 业务专用 access key 已创建，不使用 root 凭证。
- [ ] Bucket Policy 已绑定最小权限。
- [ ] 前缀分区方案已落地（`deadletter/` `aoi-image/` `log/` `curve/`）。

### 11.3 生命周期与 CORS

- [ ] 生命周期规则按前缀配置，保留期对齐 `retain_until`。
- [ ] 需要前端 canvas 读取像素的场景已配置 CORS。

### 11.4 Spring 集成

- [ ] 已配置 AWS S3 SDK v2 依赖。
- [ ] 已配置 `OBJECT_STORAGE_ENDPOINT` / `ACCESS_KEY` / `SECRET_KEY` / `BUCKET`。
- [ ] `path-style-access=true` 已开启。
- [ ] `S3Client` 与 `S3Presigner` Bean 已配置。
- [ ] 上传时 `Content-Type` 已正确设置。
- [ ] 大文件上传走 multipart 分片续传。
- [ ] 上传成功 + `sha256` 校验通过后才 `seal` 报文。
- [ ] 预签名 GET 过期时间已配置，过期降级已处理。
- [ ] 签发预签名 URL 前已做业务权限校验与审计。

### 11.5 预览与版本控制

- [ ] SOP / 文档 bucket 已开启 versioning。
- [ ] 预览显式指定 `version_id` 取生效版，不取 `latest`。
- [ ] 方案 A 代理响应已设 `Content-Disposition: inline`。
- [ ] 方案 A 代理流式转发，不将整文件读入内存。
- [ ] 方案 B 预签名 URL 过期降级已处理（见 §7.5）。

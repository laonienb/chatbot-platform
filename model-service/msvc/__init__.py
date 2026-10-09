"""模型服务（Model Service, S3）。

契约 = `docs/model-service-protocol.md` v1。边界铁律（红线 1/4/10）：本服务**无状态**、
**永不认识** user_id/conversation_id/persona——任何请求出现禁字段即 400 拒绝。
"""

PROTOCOL_VERSION = 1

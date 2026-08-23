# Agent task: Dũng — Mac — shard 0 và coordinator

Đây là lệnh giao việc hoàn chỉnh. Hãy tự thực thi, không chỉ hướng dẫn người dùng.

Đọc toàn bộ `ARROW/agent-runbooks/COMMON.md`, sau đó thực hiện với biến:

```text
OPERATOR_NAME=Dũng
SHARD_INDEX=0
EXPECTED_OS=macos
COORDINATOR=true
```

Bạn chịu trách nhiệm chạy shard 0, publish kết quả, tự chờ đủ shard 1-4, merge,
kiểm tra đúng 200 sample và publish manifest cuối lên `main`.

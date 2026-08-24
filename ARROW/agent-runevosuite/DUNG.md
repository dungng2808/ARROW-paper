# Agent task: Dũng — macOS — EvoSuite shard 0 và coordinator

Đây là lệnh giao việc hoàn chỉnh. Hãy tự thực thi, không chỉ hướng dẫn người dùng.

Đọc toàn bộ `ARROW/agent-runevosuite/COMMON.md`, sau đó chạy với:

```text
OPERATOR_NAME=Dũng
SHARD_INDEX=0
EXPECTED_OS=macos
COORDINATOR=true
```

Chạy và publish shard 0. Sau đó tự chờ shard 1–4 xuất hiện trên `origin/main`,
merge đủ 200 record, kiểm complete-case gate và báo toàn bộ số liệu cuối.


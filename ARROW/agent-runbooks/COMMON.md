# Quy trình bắt buộc cho Agent chạy qualification sample

File này dành cho coding Agent, không phải hướng dẫn để người dùng tự gõ lệnh.
Agent phải thực thi công việc đến khi hoàn thành, không chỉ mô tả lệnh.

## Biến vai trò

File vai trò sẽ cung cấp:

- `OPERATOR_NAME`: tên người chạy;
- `SHARD_INDEX`: số nguyên từ 0 đến 4;
- `EXPECTED_OS`: `macos` hoặc `windows`;
- `COORDINATOR`: `true` chỉ với Dũng.

Giá trị chung:

```text
SHARD_COUNT=5
WORKERS=2
TARGET=200
RESERVE=50
SEED=42
CANDIDATE_SHA256=7972bccb046154036617af5776debe75c973209c6921308fdb5bc47637921c1f
```

## Yêu cầu hành vi

1. Tự chạy tất cả bước bên dưới; không yêu cầu người dùng copy lệnh.
2. Không xóa, reset, restore hoặc ghi đè thay đổi local của người dùng.
3. Nếu worktree hiện tại có tracked changes, pull bị chặn, hoặc file pipeline cần
   chạy đã bị sửa local, tạo một clone sạch riêng để chạy. Không stash thay đổi
   của người dùng nếu chưa được cho phép.
4. Được phép pull và push trực tiếp `main` cho code/result thuộc quy trình này.
5. Chỉ stage đúng file kết quả của shard được giao. Không dùng `git add .`.
6. Không commit `.venv`, `Java-version`, `runs`, `repos`, build cache, log lớn,
   API key hoặc `config/pipeline.yaml` có secret.
7. Không đổi seed, filter, target, reserve, shard count hoặc shard index.
8. Không thêm `--no-resume`. Khi bị gián đoạn, chạy lại đúng run ID/output dir.
9. Không dùng kết quả generated test để quyết định giữ hoặc loại sample.
10. Không báo hoàn thành trước khi shard có đủ `preflight_audit.csv` và
    `provenance.json`, đã kiểm tra metadata và đã push result lên `main`.

## 1. Chuẩn bị repository an toàn

Tìm repository root bằng Git, không đoán tên folder là `ARROW-paper` vì máy có
thể đặt tên folder là `workspace`.

```text
git rev-parse --show-toplevel
git remote get-url origin
git status --short
```

Nếu chưa có repository, clone từ:

```text
https://github.com/dungng2808/ARROW-paper.git
```

Nếu repository có tracked changes, tạo clone sạch ở một đường dẫn mới dành riêng
cho shard. Không sửa worktree bẩn. Sau đó, trong clone sạch:

```text
git switch main
git pull --ff-only origin main
```

Xác nhận commit hiện tại có các file:

```text
ARROW/select_clean_samples.py
ARROW/requirements-selection.txt
ARROW/scripts/install-java-versions.ps1
ARROW/scripts/install-java-versions-macos.sh
ARROW/shards/clean-samples-seed42/candidate_manifest.csv
```

Đứng trong thư mục `ARROW` cho toàn bộ bước còn lại.

## 2. Kiểm tra đúng máy và candidate

- Từ chối chạy nếu hệ điều hành thực tế khác `EXPECTED_OS`.
- Tính SHA256 của
  `shards/clean-samples-seed42/candidate_manifest.csv`.
- Hash phải đúng `CANDIDATE_SHA256`. Nếu sai, pull lại `main`; không tự sửa CSV.

## 3. Tải Java local

### Windows

Agent phải thực thi bằng PowerShell, kể cả khi terminal ban đầu là `cmd`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-java-versions.ps1
```

Nếu mở PowerShell process mới sau khi cài:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
. .\Java-version\activate-java-versions.ps1
```

### macOS

```bash
chmod +x scripts/install-java-versions-macos.sh
./scripts/install-java-versions-macos.sh
source Java-version/activate-java-versions.sh
```

Xác nhận `JAVA_8_HOME`, `JAVA_11_HOME`, `JAVA_17_HOME`, `JAVA_21_HOME` đều nằm
trong `ARROW/Java-version` và cả `java -version`, `javac -version` tương ứng đều
chạy thành công.

## 4. Python tối giản và build tools

Tạo `.venv` bằng Python 3 rồi chỉ cài:

```text
requirements-selection.txt
```

Không cài `requirements.txt`; bước chọn sample không cần LLM/LiteLLM.

Xác nhận các lệnh sau hoạt động trước khi chạy shard:

```text
git --version
mvn -version
```

Gradle project ưu tiên wrapper. Nếu Git/Maven hoặc build tool thiết yếu bị thiếu,
Agent tự cài bằng phương pháp phù hợp với hệ điều hành và tiếp tục. Không được
bỏ classpath/probe/offline check để che lỗi thiếu tool.

## 5. Chạy shard

Run ID và output bắt buộc:

```text
run-id: clean-200-shard-${SHARD_INDEX}
output: runs/sample_selection/distributed/shard-${SHARD_INDEX}
```

Lệnh logic phải tương đương chính xác với:

```text
python select_clean_samples.py
  --dataset shards/clean-samples-seed42/dataset
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv
  --run-id clean-200-shard-${SHARD_INDEX}
  --output-dir runs/sample_selection/distributed/shard-${SHARD_INDEX}
  --target 200
  --reserve 50
  --seed 42
  --shard-count 5
  --shard-index ${SHARD_INDEX}
  --workers 2
  --batch-size 25
  --baseline-repeats 2
```

Agent tự chuyển cú pháp multiline sang PowerShell hoặc Bash đúng hệ điều hành.
Giữ tiến trình chạy đến khi hoàn tất. Nếu process/network bị gián đoạn, sửa lỗi
hạ tầng an toàn rồi chạy lại đúng lệnh để resume. Trong distributed mode, shard
phải xử lý hết candidate được giao; không dừng sớm khi đủ sample cục bộ.

## 6. Kiểm tra và publish shard result

Hai file bắt buộc:

```text
runs/sample_selection/distributed/shard-${SHARD_INDEX}/preflight_audit.csv
runs/sample_selection/distributed/shard-${SHARD_INDEX}/provenance.json
```

Đọc `provenance.json` và xác nhận:

```text
candidate_manifest_sha256 == CANDIDATE_SHA256
shard_count == 5
shard_index == SHARD_INDEX
processed_candidates > 0
```

Copy đúng hai file vào:

```text
ARROW/shard-results/shard-${SHARD_INDEX}/
```

Từ repository root, chỉ stage:

```text
ARROW/shard-results/shard-${SHARD_INDEX}/preflight_audit.csv
ARROW/shard-results/shard-${SHARD_INDEX}/provenance.json
```

Commit message:

```text
data: add clean sample shard ${SHARD_INDEX} (${OPERATOR_NAME})
```

Sau đó:

```text
git pull --rebase origin main
git push origin HEAD:main
```

Nếu push bị từ chối do shard khác vừa push, lặp lại pull rebase và push. Không
force-push. Xác nhận hai file đã tồn tại trên `origin/main` trước khi kết thúc.

## 7. Chỉ dành cho coordinator Dũng

Nếu `COORDINATOR=false`, bỏ qua phần này.

Sau khi publish shard 0, tiếp tục theo dõi `origin/main` cho đến khi có đủ:

```text
ARROW/shard-results/shard-0/preflight_audit.csv
ARROW/shard-results/shard-0/provenance.json
...
ARROW/shard-results/shard-4/preflight_audit.csv
ARROW/shard-results/shard-4/provenance.json
```

Việc shard khác chưa hoàn thành là trạng thái chờ bình thường, không phải lý do
kết thúc nhiệm vụ. Fetch/pull định kỳ, không ghi đè worktree của người dùng.

Khi đủ năm shard, chạy merge:

```text
python select_clean_samples.py
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv
  --run-id clean-200-merged-seed42
  --output-dir runs/sample_selection/clean-200-merged-seed42
  --target 200
  --reserve 50
  --merge-shard-dir shard-results/shard-0
  --merge-shard-dir shard-results/shard-1
  --merge-shard-dir shard-results/shard-2
  --merge-shard-dir shard-results/shard-3
  --merge-shard-dir shard-results/shard-4
```

Xác nhận:

- `selection_summary.json` có `enough_samples: true`;
- `final_manifest_200.csv` có đúng 200 data row;
- năm file `final_manifest_200_shard_I_of_5.csv` tồn tại;
- merge không báo thiếu/trùng/sai shard hoặc sai candidate hash.

Copy các output cuối vào folder tracked:

```text
ARROW/shards/clean-samples-seed42/final/
```

Tối thiểu publish:

```text
final_manifest_200.csv
reserve_manifest_50.csv
selection_summary.json
provenance.json
final_manifest_200_shard_0_of_5.csv
final_manifest_200_shard_1_of_5.csv
final_manifest_200_shard_2_of_5.csv
final_manifest_200_shard_3_of_5.csv
final_manifest_200_shard_4_of_5.csv
```

Stage đúng folder `final`, commit message
`data: publish clean 200 sample manifests`, pull rebase rồi push `main`. Báo lại
commit cuối, số eligible/final/reserve và đường dẫn `final_manifest_200.csv`.

Nếu `enough_samples` là false, không tự nới filter và không dùng generation
result để bổ sung. Báo rõ số lượng còn thiếu và giữ đầy đủ audit để điều phối run
candidate bổ sung đúng methodology.

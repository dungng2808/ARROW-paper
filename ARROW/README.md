# ARROW Pipeline

Huong dan nay dung cho pipeline `ARROW`. Pipeline nay doc input tu
`classes2test/dataset`, clone repo, tao workspace rieng, phan tich project, sinh
test bang LiteLLM qua Ollama local, chay Maven/Gradle, Adaptive Repair neu test
fail, sau do ghi report.

Pipeline nay khong phu thuoc code trong `mini-agonetest`.

## EvoSuite baseline cho RQ3

Runner `run_evosuite.py` chạy EvoSuite standalone trên đúng từng hàng của
`shards/repo_shard_05_manifest.csv`. Runner không sửa `pom.xml` hoặc
`build.gradle` của repository. Mỗi sample được kiểm tra baseline trước, sau đó
runner lấy classpath của đúng module, sinh test với seed/budget cố định, biên
dịch test cùng EvoSuite runtime và chạy bằng JUnit. Mọi timeout/failure vẫn được
ghi vào JSONL/CSV.

Tải công cụ một lần:

```bash
python3 run_evosuite.py --download-tools --setup-only
```

Khóa và kiểm tra 200 hàng mà chưa clone/build:

```bash
python3 run_evosuite.py --dry-run
```

Chạy thử một sample:

```bash
python3 run_evosuite.py \
  --run-id evosuite-smoke \
  --limit 1 \
  --workers 1 \
  --search-budget 60 \
  --keep-repo-cache
```

Chạy đủ bộ (MacBook 48 GB nên bắt đầu với 2 worker):

```bash
python3 run_evosuite.py \
  --run-id evosuite-rq3-seed42 \
  --workers 2 \
  --search-budget 120 \
  --seeds 42
```

Nếu lệnh bị dừng, chạy lại đúng `--run-id`; mặc định runner đọc
`evosuite_records.jsonl` và bỏ qua record đã hoàn tất. Kết quả chính nằm trong
`runs/evosuite/<run-id>/evosuite_records.jsonl`, bản CSV dùng để tổng hợp bảng
ở `evosuite_records.csv`, và tỷ lệ CSR/ESR/TPR/valid ở `summary.json`.

Chạy lại riêng lỗi hạ tầng/classpath sau khi đã sửa môi trường, đồng thời thay
record cũ thay vì ghi trùng:

```bash
python3 run_evosuite.py \
  --run-id evosuite-rq3-seed42 \
  --workers 2 \
  --search-budget 120 \
  --seeds 42 \
  --rerun-status TOOL_ERROR \
  --rerun-status CLASSPATH_FAILED
```

Không dùng `--no-resume` cho trường hợp này vì tùy chọn đó chạy lại toàn bộ
manifest. Một repository đã bị xóa, dependency/plugin lịch sử không còn tải
được, hoặc test cần database/Docker không được cấu hình vẫn phải được ghi nhận
là lỗi môi trường; runner không đổi các lỗi đó thành kết quả EvoSuite.

Trường `evosuite_coverage` là coverage nội bộ của objective tìm kiếm EvoSuite,
không phải line/branch coverage đo bằng JaCoCo và không phải mutation score của
PIT. Không chép trường này vào cột JaCoCo/PIT của bài báo.

Pipeline chay duoc tren ca Windows va Ubuntu. Code Java do LLM tra ve duoc
chuan hoa CRLF/LF va kiem tra block, comment, literal, dau ngoac truoc khi goi
Maven/Gradle. Neu response bi cat giua chung (`reached end of file while
parsing`), pipeline khong ghi de candidate dang dung ma tu dong generate lai
theo `llm.max_invalid_output_retries`. Gioi han output mac dinh la 4096 token
de tranh cat file Java dai o moc 2048 token.

## 1. Chuan bi moi truong

Mo PowerShell tai folder:

```powershell
cd "G:\FPT Specialized\Ki 9\SEP490\Mini-Agonetest\workspace\ARROW"
```

Cai dependency Python:

```powershell
python -m pip install -r requirements.txt
```

Kiem tra Java, Maven/Gradle va Git:

```powershell
git --version
java -version
mvn -version
gradle -version
```

Neu may co nhieu Java version, co the truyen rieng `JAVA_HOME` cho build:

```powershell
python -m src.run_pipeline --java-home "C:\Program Files\Java\jdk1.8.0_202" --count-only
```

Ubuntu:

```bash
python -m src.run_pipeline --java-home /usr/lib/jvm/java-17-openjdk-amd64 --count-only
```

Neu muon auto-detect Java theo tung repo nhung JDK nam o thu muc rieng cua may,
sua `build.java_homes` trong `config/pipeline.yaml`:

```yaml
build:
  java_default:
    windows: 'D:\Java\jdk-21'
    linux: '/usr/lib/jvm/java-21-openjdk-amd64'
  java_homes:
    java-8:
      windows: 'D:\Java\jdk1.8.0_202'
      linux: '/usr/lib/jvm/java-8-openjdk-amd64'
    java-11:
      windows: 'D:\Java\jdk-11'
      linux: '/usr/lib/jvm/java-11-openjdk-amd64'
    java-17:
      windows: 'D:\Java\jdk-17'
      linux: '/usr/lib/jvm/java-17-openjdk-amd64'
```

Key co the viet dang `java-17`, `jdk-17`, hoac chi `"17"`. Khi repo detect can
Java 17 thi pipeline dung path trong `java_homes.java-17`. Neu path khong ton
tai, pipeline tu tim JDK dung version qua `JAVA_17_HOME`, `JAVA17_HOME`,
`JDK_17_HOME`, `JAVA_HOME`, `/usr/lib/jvm`, cac thu muc JDK pho bien tren
Windows, hoac `JAVA_VERSIONS_HOME`. Sau do pipeline moi fallback ve
`java_default` va Java he thong.

Voi project legacy khai bao Java 7 tro xuong, neu khong co JDK khop chinh xac,
pipeline uu tien JDK 8 truoc `java_default`. Cach nay tranh JVM crash voi cac
build plugin cu nhu JaCoCo 0.7.x va Surefire 2.12.x khi Java he thong la 17/21.

Neu project khong khai bao Java source/target, pipeline doc version trong
`gradle/wrapper/gradle-wrapper.properties`. Vi du Gradle 4.x se chay bang JDK 8
thay vi Java 17/21, tranh loi `Unsupported class file major version 61`.

Mot so Gradle build goi `git describe` de tao version. Pipeline chi voi cac repo
nay se fetch day du history/tag va copy `.git` vao workspace rieng. Cac repo
khong dung Git trong build van giu shallow clone de tranh tang thoi gian va dung
luong tren ca Windows va Ubuntu.

## 2. Chay Ollama, OpenAI va LiteLLM

Pipeline bat buoc goi model qua `src/llm_client.py` bang LiteLLM. Khong goi
Ollama truc tiep trong pipeline.

Vi du model trong `config/pipeline.yaml`:

```yaml
llm:
  provider: litellm
  agents:
    - name: qwen-coder-1.5b
      model: ollama/qwen2.5-coder:1.5b
      api_base: http://localhost:11434
    - name: gpt-4.1-mini
      model: openai/gpt-4.1-mini
      api_key_env: OPENAI_API_KEY
```

`api_key_env` la ten bien moi truong chua API key, khong phai API key that.
Khong ghi `sk-...` truc tiep vao YAML. Dat key truoc khi khoi dong pipeline
hoac dashboard de process con ke thua bien moi truong nay.

PowerShell:

```powershell
$env:OPENAI_API_KEY="sk-..."
python -m dashboard.server --host 127.0.0.1 --port 8765
```

Ubuntu:

```bash
export OPENAI_API_KEY="sk-..."
python -m dashboard.server --host 127.0.0.1 --port 8765
```

Chay rieng GPT tu command line:

```powershell
python -m src.run_pipeline --agent gpt-4.1-mini --start-index 0 --limit 1
```

Chuan bi Ollama:

```powershell
ollama serve
ollama pull qwen2.5-coder:1.5b
```

## 3. Cau hinh quan trong trong YAML

File cau hinh chinh:

```text
config/pipeline.yaml
```

Phan input:

```yaml
input:
  mode: sample # sample | project
  project_ids: []
  project_shard_file: null
  samples_per_project: 1 # positive integer | all
  sample_order: sorted
```

- `mode: sample`: `--limit` tinh theo tung JSON sample.
- `mode: project`: `--limit` tinh theo project folder.
- `samples_per_project: all`: lay tat ca JSON sample trong moi project da chon.

Phan repo:

```yaml
repo:
  clone_repo: true
  repos_dir: repos
  checkout_commit: true
  delete_after_report: true
```

- `clone_repo: true`: clone repo ve `repos/<project_id>/`.
- `delete_after_report: true`: sau khi da ghi report, xoa repo cache de khong nang may.

Phan output:

```yaml
output:
  root_dir: runs
  layout: repo_sample # repo_sample | run_shard
  repo_folder_name: repo_name # repo_name | owner_repo | project_id
  on_repo_name_conflict: fail # fail | append_project_id
```

- `layout: repo_sample`: output nam theo ten repo va sample id.
- Vi du repo `https://github.com/habernal/semeval2018-task12`, sample `100035986_1`:

```text
runs/semeval2018-task12/100035986_1/
```

- Neu muon quay ve kieu benchmark cu:

```yaml
output:
  layout: run_shard
```

Phan report:

```yaml
report:
  primary_format: json
  write_per_experiment_json: true
  write_shard_jsonl: true
  write_csv_per_run: false
  json_records_dir: records
  merged_dir: merged
```

- JSON/JSONL la output chinh.
- CSV chi sinh khi merge/export cuoi cung, hoac khi bat `write_csv_per_run: true`.

Phan cleanup:

```yaml
cleanup:
  delete_experiment_workspace_after_report: true
  delete_repo_cache_after_run: true # legacy alias; repo.delete_after_report is preferred
  keep_failed_workspaces: false
```

- `delete_experiment_workspace_after_report: true`: xoa workspace chay that sau khi xong.
- `keep_failed_workspaces: true`: neu can debug loi thi giu lai workspace fail.
- Khi can giu repo/workspace tam thoi de kiem tra, dung CLI flag:

```powershell
--keep-repo-cache
--keep-workspace
```

## 4. Tham số đầu vào của CLI

Lệnh chính:

```powershell
python -m src.run_pipeline [options]
```

Các tham số chọn input:

| Tham số | Ý nghĩa | Ví dụ |
|---|---|---|
| `--project-id` | Chọn đúng một project/repo theo folder id trong `classes2test/dataset`. | `--project-id 13899` |
| `--sample-file` | Chọn đúng một JSON sample trong project đã chọn. Thường dùng cùng `--project-id`. | `--sample-file 13899_8.json` |
| `--start-index` | Vị trí bắt đầu trong danh sách input sau khi sort. Dùng để chia đoạn hoặc chạy tiếp. | `--start-index 0` |
| `--limit` | Số input sẽ chạy từ `start-index`. Trong `mode: sample` tính theo JSON sample; trong `mode: project` tính theo project folder. Giá trị `0` nghĩa là chạy toàn bộ phần còn lại. | `--limit 10` |
| `--repo-shard` | File `.txt` chứa danh sách project id, mỗi dòng một repo. Dùng khi chia cho nhiều người chạy theo repo. | `--repo-shard shards\repo_shard_00.txt` |
| `--shard-id` | Tên phần việc/người chạy. Hiện chủ yếu lưu metadata trong report. | `--shard-id person_00` |

Các tham số kiểm tra input mà không chạy thật:

| Tham số | Ý nghĩa |
|---|---|
| `--count-only` | Đếm tổng project folder và tổng JSON sample, không clone/generate/build. |
| `--list-inputs` | In danh sách input sẽ chạy theo `--start-index`, `--limit`, `--project-id`, `--repo-shard`. |
| `--dry-run` | In kế hoạch workspace/output path, không clone repo, không gọi LLM, không build. |

Các tham số chọn model/prompt:

| Tham số | Ý nghĩa | Ví dụ |
|---|---|---|
| `--agent` | Chạy một agent/model cụ thể trong `config/pipeline.yaml`. Có thể dùng agent name như `qwen-coder-1.5b` hoặc model alias như `qwen2.5-coder:1.5b`. Có thể truyền nhiều lần. | `--agent qwen-coder-1.5b` |
| `--generation-prompt` | Chạy một prompt generation cụ thể. Có thể truyền nhiều lần. | `--generation-prompt zero-shot` |

Các tham số build/runtime:

| Tham số | Ý nghĩa | Ví dụ |
|---|---|---|
| `--java-home` | Truyền `JAVA_HOME` riêng cho Maven/Gradle command. Hữu ích khi sample cần Java 8. | `--java-home "C:\Program Files\Java\jdk1.8.0_202"` |
| `--skip-metrics` | Bỏ qua coverage/mutation/smell metrics. Dùng cho smoke test nhanh. | `--skip-metrics` |

Coverage/mutation metrics:

- Sau khi `target_test_passed` và `module_tests_passed` đều true, pipeline chạy
  JaCoCo, PIT, và tsDetect cho Maven project nếu các cờ tương ứng trong
  `metrics:` đang bật ở `config/pipeline.yaml`.
- Metrics được ghi vào `metrics_report.json`, `coverage_verification.json`,
  `coverage_build_output.txt`, `mutation_verification.json`, và
  `mutation_build_output.txt` trong experiment folder.
- Các cột paper-style trong `result.json` sẽ được điền từ JaCoCo/PIT nếu tool
  chạy thành công:
  `Branch_Coverage%`, `Line_Coverage%`, `Method_Coverage%`,
  `Mutation_Score%`.
- Nếu metrics fail do plugin/dependency/tool, test result vẫn giữ nguyên; lỗi
  metrics được ghi ở `coverage_error`, `mutation_error`, hoặc `smell_error`.
- Test smell dùng `classes2test/AgoneTest/TestSmellDetector.jar` theo cách của
  paper pipeline: tạo `test_smells/pathToInputFile.csv`, chạy jar, rồi parse
  `Output_TestSmellDetection*.csv` để điền các cột smell trong `result.json`.
- Run cũ có `workspace_deleted: true` cần chạy lại sample để sinh metrics.

Các tham số test không cần LLM thật:

| Tham số | Ý nghĩa |
|---|---|
| `--mock-llm-smoke` | Sinh một test rỗng nhỏ để kiểm tra clone/analyze/build/report mà không gọi Ollama. |
| `--mock-llm-output` | Dùng nội dung một file làm fake response của LLM. |

Quy uoc ten generated test:

- Pipeline sinh ten class theo mau `<FocalClassName>Test_<hash>`.
- Neu LLM tra ve mot bien the an toan nhu
  `<FocalClassName>GeneratedTest_<hash>` hoac
  `<FocalClassName>AgoneGeneratedTest_<hash>`, pipeline se normalize ve ten
  required truoc khi validate/ghi file.
- Neu LLM tra ve ten class khong lien quan, output van bi reject.

Các tham số cleanup/debug:

| Tham số | Ý nghĩa |
|---|---|
| `--keep-repo-cache` | Không xoá repo cache trong `repos/<project_id>/` sau khi report được ghi. |
| `--keep-workspace` | Không xoá workspace experiment sau khi chạy xong. Dùng khi cần debug generated test/build output. |

Các tham số merge report:

| Tham số | Ý nghĩa | Ví dụ |
|---|---|---|
| `--merge-reports` | Không chạy pipeline mới; chỉ merge các `experiments.jsonl` đã có. | `--merge-reports` |
| `--runs-dir` | Folder chứa các run/repo output cần merge. | `--runs-dir runs` |
| `--output-dir` | Folder ghi report merge cuối. | `--output-dir runs\merged` |

Quan hệ với `config/pipeline.yaml`:

- Nếu dùng `--project-id` + `--sample-file`, pipeline chạy đúng một sample, bỏ qua `input.mode`.
- Nếu dùng `--repo-shard`, pipeline chỉ chọn repo nằm trong shard file.
- Nếu không truyền `--project-id`, `--sample-file`, `--repo-shard`, pipeline chọn input theo block `input:` trong YAML.
- `input.mode: sample`: `--start-index` và `--limit` áp dụng trên danh sách JSON sample.
- `input.mode: project`: `--start-index` và `--limit` áp dụng trên danh sách project folder, rồi lấy sample theo `samples_per_project`.
- `samples_per_project: all`: lấy toàn bộ JSON sample trong mỗi repo đã chọn.

Ví dụ chạy đúng một sample:

```powershell
python -m src.run_pipeline `
  --project-id 13899 `
  --sample-file 13899_8.json `
  --skip-metrics `
  --agent qwen-coder-1.5b `
  --generation-prompt zero-shot
```

Ví dụ chạy 10 sample đầu tiên:

```powershell
python -m src.run_pipeline --start-index 0 --limit 10 --skip-metrics
```

Ví dụ chạy shard repo của người số 0:

```powershell
python -m src.run_pipeline `
  --repo-shard shards\repo_shard_00.txt `
  --shard-id person_00 `
  --skip-metrics
```

## 5. Cac lenh chay thuong dung

Dem tong so project folder va JSON sample:

```powershell
python -m src.run_pipeline --count-only
```

List input, khong clone repo, khong generate, khong build:

```powershell
python -m src.run_pipeline --list-inputs --start-index 0 --limit 10
```

Xem plan workspace ma khong clone/generate/build:

```powershell
python -m src.run_pipeline --dry-run --start-index 0 --limit 3
```

Chay 1 sample cu the:

```powershell
python -m src.run_pipeline `
  --project-id 100035986 `
  --sample-file 100035986_1.json `
  --skip-metrics `
  --agent qwen-coder-1.5b `
  --generation-prompt zero-shot
```

Chay 10 JSON sample bat dau tu index 0:

```powershell
python -m src.run_pipeline --start-index 0 --limit 10 --skip-metrics
```

Chay voi Java rieng:

```powershell
python -m src.run_pipeline `
  --project-id 100035986 `
  --sample-file 100035986_1.json `
  --skip-metrics `
  --java-home "C:\Program Files\Java\jdk1.8.0_202"
```

## 6. Chay smoke khong can Ollama

Dung khi muon test pipeline clone/analyze/build/report nhung khong muon goi LLM
that:

```powershell
python -m src.run_pipeline `
  --project-id 100035986 `
  --sample-file 100035986_1.json `
  --skip-metrics `
  --mock-llm-smoke `
  --agent qwen-coder-1.5b `
  --generation-prompt zero-shot `
  --java-home "C:\Program Files\Java\jdk1.8.0_202"
```

Dung response fake tu file:

```powershell
python -m src.run_pipeline `
  --project-id 100035986 `
  --sample-file 100035986_1.json `
  --skip-metrics `
  --mock-llm-output path\to\fake_response.java
```

## 7. Output report JSON/JSONL

Mac dinh moi sample tao folder theo repo:

```text
runs/<repo_name>/<sample_id>/
```

Vi du:

```text
runs/adbcj/13899_8/
runs/semeval2018-task12/100035986_1/
```

Output report chinh:

```text
runs/<repo_name>/<sample_id>/reports/
  records/
    experiments.jsonl
    <sample_id>/<agent_name>/<generation_prompt>/result.json
  run_summary.json
```

- `result.json`: report day du cua mot experiment.
- `experiments.jsonl`: moi dong la mot experiment, dung de merge nhanh ve sau.
- `run_summary.json`: tom tat lan chay sample do.

Field dang chu y trong `result.json` va `experiments.jsonl`:

- `project_id`, `sample_file`, `repository_url`
- `repo_name`, `repo_owner`, `repo_folder`, `sample_id`, `output_layout`
- `agent_name`, `model`, `generation_prompt_strategy`
- `build_tool`, `module_path`
- `baseline_state`, `baseline_error_signatures`
- `initial_failure_state`, `final_failure_state`
- `initial_failure_origin`, `final_failure_origin`
- `repair_status`, `repair_stopped_reason`
- `target_test_passed`, `module_tests_passed`, `test_passed`
- `started_at`, `finished_at`, `elapsed_seconds`
- `target_passed_at`, `target_pass_elapsed_seconds`
- `module_passed_at`, `module_pass_elapsed_seconds`
- `first_passed_at`, `first_pass_elapsed_seconds`
- `repo_cache_path`, `repo_cache_deleted`
- `experiment_workspace`, `workspace_deleted`
- `checkpoint_directory`
- `compile_errors`, `test_failures`, `test_errors`
- `elapsed_seconds`, `error`

Neu `repo_cache_deleted=True` va `workspace_deleted=True` thi pipeline da don dep
repo/workspace sau khi ghi report.

## 8. Merge report va tao CSV trung binh

Sau khi chay nhieu sample, merge tat ca JSONL:

```powershell
python -m src.run_pipeline --merge-reports --runs-dir runs --output-dir runs\merged
```

Output:

```text
runs/merged/
  experiments_merged.jsonl
  output_agone_classes_lite.csv
  output_agone_mean_lite.csv
  merge_summary.json
```

Mean CSV co them cac cot timing:

- `avg_elapsed_seconds`
- `avg_target_pass_elapsed_seconds`
- `avg_module_pass_elapsed_seconds`
- `avg_first_pass_elapsed_seconds`

## 9. Adaptive Repair

Adaptive Repair chi sua generated test file trong workspace rieng. No khong duoc
sua:

- focal class;
- production code;
- existing human-written tests;
- `pom.xml`;
- `build.gradle`;
- dependency config;
- wrapper files.

Quyet dinh repair dua tren:

- failure state;
- normalized error signature;
- generated-code hash;
- Maven/Gradle verification that;
- checkpoint va rollback;
- prompt fallback;
- gioi han attempt.

Khong dung `error_count`, `compile_errors`, `test_failures`, `test_errors` de
quyet dinh progress hay rollback. Cac truong so nay chi dung cho report.

## 10. Test pipeline

Chay compile check:

```powershell
python -m compileall src tests
```

Chay automated tests:

```powershell
python -m pytest tests
```

## 11. Dashboard UI

Dashboard chay bang Python stdlib, khong can npm/FastAPI:

```powershell
python -m dashboard.server --host 127.0.0.1 --port 8765
```

Mo trinh duyet:

```text
http://127.0.0.1:8765
```

Dashboard co the chon project/sample/agent/prompt, chay pipeline, set cac chi
so bounded retry cua Adaptive Repair, xem log, doc `result.json`, metrics cua
JaCoCo/PIT/tsDetect, `repair_summary.json`, va timeline checkpoint. Project list
doc toan bo `classes2test/dataset`. Pipeline clone/analyze repo truoc, detect
Java version tu Maven/Gradle/project files, neu version co trong `java_homes` thi
dung JDK tuong ung; neu khong detect duoc hoac khong co mapping thi dung
`java_default`; neu `java_default` khong hop le thi dung Java default cua may.
O `JDK version map` tren dashboard cho phep nhap moi dong mot mapping
`java-17: D:\Java\jdk-17`; dashboard ghi mapping nay vao `build.java_homes` cua
runtime config cho lan chay do. O `Java default` ghi fallback JDK cho run.
Neu muon ep mot run dung rieng mot JDK, dien `Override JAVA_HOME`. Khi run ket
thuc, bang Experiments tu refresh. Neu dang run va muon dung, bam Stop trong run
list; dashboard se dung ca process tree cua pipeline/Maven/Gradle. Log hien ro
phase, Java selection, command Maven/Gradle, verification result, va tung repair
attempt.

De chia cho 4 nguoi tren dashboard, moi nguoi chay dashboard tren may cua minh,
chon `Run scope = Shard batch`, roi chon mot file shard khac nhau:

```text
person_00 -> repo_shard_00.txt
person_01 -> repo_shard_01.txt
person_02 -> repo_shard_02.txt
person_03 -> repo_shard_03.txt
```

Nen set `Input mode = Project`, `Samples/project = 1` neu muon moi repo lay mot
sample dau tien, hoac `Samples/project = all` neu muon lay tat ca sample trong
moi repo. Set `Start index = 0` va `Limit = 0` de chay het shard do.

## 12. Luu y ve dung luong may

Mac dinh pipeline da cau hinh:

```yaml
repo:
  delete_after_report: true

cleanup:
  delete_experiment_workspace_after_report: true
```

Nen sau khi chay xong va report duoc ghi, repo clone va workspace se bi xoa.
Neu can debug file generated test/build output trong workspace, chay them:

```powershell
--keep-workspace --keep-repo-cache
```


Nhưng command này không chạy toàn bộ project 766548. Vì --limit mặc định là 1, và config hiện tại input.mode: sample, nên nó sẽ lấy 1 sample đầu tiên trong folder 766548, khả năng là 766548_0.json.
python -m src.run_pipeline `
  --project-id 100262257 `
  --agent qwen-coder-1.5b `
  --generation-prompt zero-shot

Nếu muốn chạy đúng một sample cụ thể:
python -m src.run_pipeline `
  --project-id 766548 `
  --sample-file 766548_0.json `
  --agent qwen-coder-1.5b `
  --generation-prompt zero-shot



Nếu muốn chạy 10 sample đầu của project đó:
python -m src.run_pipeline `
  --project-id 32578 `
  --limit 10 `
  --agent qwen-coder-1.5b `
  --generation-prompt zero-shot


Không chạy full nhiều lần nếu không cần. Khi cần debug mutation, chạy một lần:
python -m src.run_pipeline `
  --project-id 100262257 `
  --sample-file 100262257_9.json `
  --agent qwen-coder-1.5b `
  --generation-prompt zero-shot `
  --keep-workspace `
  --keep-repo-cache


python -m dashboard.server --host 127.0.0.1 --port 8765

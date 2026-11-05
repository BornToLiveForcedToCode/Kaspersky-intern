# vLLM CPU on Kubernetes 

Учебный проект для тестового задания Data Engineer команды исследования технологий машинного обучения.

Решение разворачивает OpenAI-compatible API для модели `Qwen/Qwen2.5-0.5B-Instruct` на CPU в локальном Kubernetes-кластере Minikube. Основной inference-контейнер и контейнер скачивания модели работают от непривилегированного пользователя. Веса хранятся на PVC; параметры сервера передаются через ConfigMap, токены — через Kubernetes Secret.

## 1. Архитектура и выбранный стек

| Компонент | Реализация |
|---|---|
| Kubernetes | Minikube, профиль `vllm-lab` |
| Контейнерный runtime | Docker Desktop |
| Inference framework | vLLM `0.33.0` |
| Compute backend | CPU |
| Модель | `Qwen/Qwen2.5-0.5B-Instruct` |
| Основной образ | `vllm-cpu:0.33.0`, на базе `vllm/vllm-openai-cpu:v0.33.0` |
| Образ скачивателя | `vllm-downloader:0.33.0`, на базе `python:3.12-slim-bookworm` |
| Хранилище весов | PVC `model-cache` |
| Настройки inference | ConfigMap `vllm-config` |
| HF/API credentials | Secret `vllm-secrets` |
| Деплой | Kustomize (`kubectl apply -k`) |

Модель 0.5B выбрана для работы на локальной машине с 16 ГБ RAM. Конфигурация стенда ограничена ресурсами Minikube: 4 CPU и 6 ГиБ памяти. Inference выполняется на CPU, без GPU.

## 2. Структура репозитория

```text
.
├── docker/
│   ├── Dockerfile
│   ├── Dockerfile.downloader
│   ├── requirements.txt
│   ├── downloader-requirements.txt
│   └── verify_lock.py
├── k8s/
│   ├── configmap.yaml
│   ├── deployment.yaml
│   ├── namespace.yaml
│   ├── pvc.yaml
│   ├── service.yaml
│   └── kustomization.yaml
├── load-tests/
│   ├── benchmark.py
│   ├── results-concurrency-1-repeat4.csv
│   └── results-concurrency-2.csv
├── scripts/
│   └── lock-vllm.ps1
├── src/
│   ├── download_model.py
│   └── logging_config.py
├── .dockerignore
├── .gitignore
└── README.md
```

`api-token.txt` создаётся локально для запуска клиента и не должен попадать в репозиторий.

## 3. Требования

Проверенная локальная среда:

- Windows 11;
- Docker Desktop;
- Minikube с Docker driver;
- `kubectl`;
- PowerShell;
- Python 3.13 для запуска клиентского benchmark (сам benchmark использует стандартную библиотеку Python).

Пример запуска Minikube с ресурсами, использованными в этом проекте:

```powershell
minikube start -p vllm-lab --driver=docker --cpus=4 --memory=6144 --disk-size=30g
kubectl config use-context vllm-lab
kubectl get nodes
```

Ожидается, что узел Minikube будет в состоянии `Ready`.

## 4. Python dependency lock и сборка образов

### 4.1. Основной vLLM-образ

`docker/requirements.txt` содержит полный `pip freeze --all` из фиксированного CPU-образа vLLM, включая транзитивные зависимости. Dockerfile проверяет этот lock-файл при сборке и не переустанавливает/не обновляет зависимости базового образа.

Сборка:

```powershell
docker build -f docker/Dockerfile -t vllm-cpu:0.33.0 .
```

Проверка lock-файла выполнялась внутри Linux-контейнера, а не системным Python Windows:

```powershell
$projectPath = (Get-Location).Path

docker run --rm `
  --entrypoint python `
  --mount "type=bind,source=$projectPath,target=/workspace,readonly" `
  --workdir /workspace `
  vllm-cpu:0.33.0 `
  docker/verify_lock.py docker/requirements.txt
```

Результат проверки в использованном окружении:

```text
Verified 196 pinned Python distributions.
```

### 4.2. Отдельный rootless-образ скачивателя

Скачивание модели вынесено из основного inference-контейнера в отдельный `initContainer`. Он использует отдельный лёгкий образ и собственный файл зависимостей:

```text
docker/Dockerfile.downloader
docker/downloader-requirements.txt
```

Сборка:

```powershell
docker build -f docker/Dockerfile.downloader -t vllm-downloader:0.33.0 .
```

Проверка пользователя образа:

```powershell
docker image inspect vllm-downloader:0.33.0 --format '{{.Config.User}}'
```

Ожидаемый результат:

```text
10001:10001
```

Проверка импорта библиотеки загрузки:

```powershell
docker run --rm `
  --entrypoint python `
  vllm-downloader:0.33.0 `
  -c "from huggingface_hub import snapshot_download; print('Downloader OK')"
```

Оба локальных образа загружаются в Minikube:

```powershell
minikube image load -p vllm-lab vllm-cpu:0.33.0
minikube image load -p vllm-lab vllm-downloader:0.33.0
```

## 5. Secret и развёртывание

Сначала создаётся namespace:

```powershell
kubectl apply -f .\k8s\namespace.yaml
```

Для публичной модели HF token можно оставить пустым. Для gated/private модели вместо пустой строки передаётся Hugging Face read token.

Следующая команда создаёт локальный API token, сохраняет его в `api-token.txt` и применяет Secret. Выполнять из корня репозитория:

```powershell
$apiToken = [guid]::NewGuid().ToString('N')
$apiToken | Set-Content -NoNewline .\api-token.txt

kubectl create secret generic vllm-secrets -n llm `
  --from-literal=HF_TOKEN= `
  --from-literal=API_TOKEN=$apiToken `
  --dry-run=client -o yaml | kubectl apply -f -
```


Сборка и загрузка образов должны быть выполнены до применения Deployment. Развёртывание:

```powershell
kubectl apply -k .\k8s\
kubectl rollout status deployment/vllm-api -n llm --timeout=10m
kubectl get pods,svc,pvc -n llm
```

`kubectl apply -k` использует `k8s/kustomization.yaml`.

### 5.1. Rootless и безопасность контейнеров

Основной контейнер и `initContainer` запускаются с UID/GID `10001`. В Kubernetes заданы:

- `runAsNonRoot: true`;
- `allowPrivilegeEscalation: false`;
- `readOnlyRootFilesystem: true`;
- `capabilities.drop: ["ALL"]`;
- `seccompProfile: RuntimeDefault` на уровне Pod.

Для проверки настроек запущенного Pod:

```powershell
$pod = kubectl get pods -n llm -l app=vllm-api -o jsonpath='{.items[0].metadata.name}'
$podInfo = kubectl get pod $pod -n llm -o json | ConvertFrom-Json

$podInfo.spec.initContainers |
  Select-Object name, image, securityContext

$podInfo.spec.containers |
  Select-Object name, image, securityContext
```

## 6. Скачивание и хранение модели

`src/download_model.py` запускается как `initContainer` из отдельного образа `vllm-downloader:0.33.0`.

Алгоритм:

1. Проверяет marker-файл `.download_complete` и наличие `config.json` в каталоге модели.
2. Если кэш полный, завершает работу без сетевого скачивания.
3. Если модели нет, использует `huggingface_hub.snapshot_download` для загрузки весов в смонтированный `/models`.
4. После успешной загрузки создаёт marker-файл.

Каталог модели на PVC:

```text
/models/Qwen2.5-0.5B-Instruct
```

Проверка логов initContainer:

```powershell
$pod = kubectl get pods -n llm -l app=vllm-api -o jsonpath='{.items[0].metadata.name}'
kubectl logs $pod -n llm -c model-downloader --tail=100
```

При повторном запуске уже заполненного PVC ожидается сообщение:

```text
Model cache is complete; skipping download for Qwen/Qwen2.5-0.5B-Instruct
```

Основной vLLM-контейнер использует `HF_HUB_OFFLINE=1` и `TRANSFORMERS_OFFLINE=1`, загружая модель локально из PVC. Не удаляй PVC `model-cache`, если требуется сохранить скачанные веса.

## 7. Конфигурация vLLM

Параметры inference задаются через `k8s/configmap.yaml`; менять их можно без пересборки Docker-образа. Текущая проверенная конфигурация включает:

| Параметр | Значение |
|---|---:|
| `dtype` | `auto` |
| `max_model_len` | 1024 |
| `max_num_seqs` | 1 |
| `max_num_batched_tokens` | 1024 |
| `tensor_parallel_size` | 1 |
| `served_model_name` | `Qwen/Qwen2.5-0.5B-Instruct` |

В Deployment также используются `--enforce-eager` и `--kv-cache-memory-bytes 268435456` (256 МиБ). Для CPU-контейнера задано `OMP_NUM_THREADS=2` и `VLLM_CPU_OMP_THREADS_BIND=nobind`.

Ограничения ресурсов основного контейнера: requests — 2 CPU / 2 GiБ RAM; limits — 4 CPU / 5 GiБ RAM. Значения подобраны под локальную машину и не являются универсальными production-рекомендациями.

## 8. Проверка API

В первом окне PowerShell запусти port-forward и оставь окно открытым:

```powershell
kubectl port-forward -n llm service/vllm-api 8000:8000
```

Во втором окне PowerShell, из корня проекта:

```powershell
$token = (Get-Content .\api-token.txt -Raw).Trim()
$headers = @{ Authorization = "Bearer $token" }

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/v1/models" `
  -Headers $headers
```

Проверка health endpoint:

```powershell
curl.exe -i http://127.0.0.1:8000/health
```

Тестовый запрос:

```powershell
$payload = @{
  model = 'Qwen/Qwen2.5-0.5B-Instruct'
  messages = @(
    @{
      role = 'user'
      content = 'Кратко объясни, что такое Kubernetes.'
    }
  )
  max_tokens = 80
  temperature = 0.2
}

$json = $payload | ConvertTo-Json -Depth 8 -Compress
$body = [System.Text.Encoding]::UTF8.GetBytes($json)

$result = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8000/v1/chat/completions' `
  -Method Post `
  -Headers $headers `
  -ContentType 'application/json; charset=utf-8' `
  -Body $body

$result.choices[0].message.content
```

Для API-запросов к защищённым endpoints требуется `Authorization: Bearer <API_TOKEN>`. В проверке проекта запрос без корректного токена к endpoint генерации вернул HTTP `401 Unauthorized`.

## 9. Privacy-safe logging

В запуске vLLM используется middleware `logging_config.audit_middleware`, а также:

```text
--disable-log-requests
--disable-log-outputs
```

Middleware регистрирует факт обращения к модели и метаданные запроса (request ID, HTTP method, status, duration). Содержимое prompt и generated response, а также значение Authorization не должны попадать в stdout/stderr.

Проверка:

```powershell
kubectl logs deployment/vllm-api -n llm -c vllm --since=10m --tail=100
```

Во время тестирования в логах наблюдались записи `inference_request` с `request_id`, методом, HTTP-статусом и длительностью, без текста prompt/response.

## 10. Нагрузочное тестирование (bonus 3)

Скрипт `load-tests/benchmark.py` выполняет потоковые Chat Completions запросы, использует финальное событие `usage` для подсчёта токенов и сохраняет только метрики в CSV. Тексты prompt и generated response не печатаются и не записываются в CSV.

Предварительные условия:

1. Pod `vllm-api` находится в состоянии `1/1 Running`.
2. Port-forward из раздела 8 активен.
3. В корне проекта есть `api-token.txt`.

Базовый прогон — 4 измеряемых запроса на сценарий и 1 warm-up:

```powershell
python .\load-tests\benchmark.py `
  --repeats 4 `
  --warmup 1 `
  --concurrency 1 `
  --output .\load-tests\results-concurrency-1-repeat4.csv
```

Параллельная нагрузка — 4 измеряемых запроса на сценарий, concurrency 2:

```powershell
python .\load-tests\benchmark.py `
  --repeats 4 `
  --warmup 1 `
  --concurrency 2 `
  --output .\load-tests\results-concurrency-2.csv
```

В каждом прогоне используются короткий и более длинный prompt-сценарии. `concurrency` задаёт число клиентских запросов, выполняемых одновременно. Текущий vLLM scheduler ограничен `max_num_seqs: 1`.

### 10.1. Результаты измерений

Результаты ниже получены на локальном CPU-only стенде, модель `Qwen/Qwen2.5-0.5B-Instruct`, vLLM `0.33.0`. В каждом сценарии — 4 измеряемых запроса после 1 warm-up.

| Prompt | Concurrency | TTFT p50, с | TTFT p95, с | Latency p50, с | Latency p95, с | Aggregate output throughput, ток/с | Requests/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| Короткий | 1 | 1.017 | 1.058 | 2.859 | 2.918 | 11.139 | 0.351 |
| Короткий | 2 | 3.567 | 4.485 | 5.705 | 6.393 | 12.356 | 0.343 |
| Длинный | 1 | 1.875 | 1.943 | 4.496 | 4.799 | 9.637 | 0.220 |
| Длинный | 2 | 5.918 | 6.174 | 8.378 | 8.787 | 10.578 | 0.235 |

**Методика метрик:**

- TTFT — клиентское время от начала HTTP-запроса до первого непустого текстового фрагмента в stream.
- Latency — полное клиентское время от начала запроса до завершения stream.
- Aggregate output throughput — сумма completion tokens в прогоне, делённая на wall-clock время всего прогона.
- Requests/s — число измеряемых запросов, делённое на wall-clock время прогона.
- p50 — медиана измерений. Скрипт использует nearest-rank p95; при четырёх наблюдениях p95 равен максимальному наблюдению.

Результаты — небольшая локальная выборка, а не универсальный performance benchmark. TTFT и latency включают клиентскую сторону и локальный `kubectl port-forward`. Для более устойчивой оценки рекомендуется увеличить число измерений до 10–30 и повторить тест при одинаковой загрузке CPU и памяти. Первая exploratory серия с тремя запросами не используется в сравнительной таблице.

### 10.2. Анализ и направления настройки

#### A. Большой поток коротких запросов

В эксперименте переход от concurrency 1 к concurrency 2 увеличил TTFT p50 с 1.017 до 3.567 с для короткого сценария. Latency p50 выросла с 2.859 до 5.705 с, при этом aggregate output throughput увеличился с 11.139 до 12.356 ток/с. Requests/s практически не вырос.

Это согласуется с тем, что `max_num_seqs` в текущем ConfigMap равен 1: клиентские запросы могут поступать одновременно, но scheduler не обрабатывает более одной последовательности за итерацию.

Что стоит проверить экспериментально:

1. Постепенно увеличить `max_num_seqs` (например, 2, затем 4), измеряя throughput и p95 latency. Это может позволить больший batch, но увеличивает конкуренцию за CPU и память.
2. Подобрать `max_num_batched_tokens`. Параметр ограничивает число токенов, обрабатываемых за одну итерацию scheduler; слишком низкое значение может ограничивать batch, слишком высокое — увеличивать нагрузку и задержки.
3. Подобрать `OMP_NUM_THREADS` и CPU requests/limits под доступные ядра. Большее число потоков не гарантирует ускорения из-за конкуренции потоков и CPU throttling.
4. Проверить поведение prefix caching, если короткие запросы имеют повторяющийся общий префикс.

Ожидаемый компромисс: увеличение batch может повысить совокупный throughput, но ухудшить latency отдельного запроса. Значения следует выбирать по повторным измерениям.

#### B. Мало запросов, но очень большой контекст

Для длинного сценария concurrency 2 увеличил TTFT p50 с 1.875 до 5.918 с и latency p50 с 4.496 до 8.378 с. Aggregate output throughput изменился с 9.637 до 10.578 ток/с.

Что стоит проверить экспериментально:

1. `max_model_len` — увеличить только если необходимо принимать более длинные последовательности. Это верхнее ограничение длины prompt плюс generated tokens; больший контекст повышает требования к памяти.
2. `max_num_batched_tokens` — подобрать бюджет scheduler для prefill длинных prompts. Более высокий бюджет может уменьшить число prefill-итераций, но увеличить пиковую нагрузку.
3. Сохранить chunked prefill и проверить влияние размера токенного бюджета: chunked prefill делит длинный prefill на части в пределах доступного бюджета итерации.
4. Оставить `max_num_seqs` небольшим (например, 1–2) для длинных контекстов, чтобы не создавать чрезмерную конкуренцию за CPU и KV cache.
5. Увеличивать KV cache только после проверки доступной RAM и фактического потребления. В текущем запуске задано 256 МиБ KV cache; увеличение требует свободной памяти в Pod/Minikube.

Prefix caching может помогать, когда длинные запросы повторно используют одинаковый префикс. Он не устраняет стоимость обработки новой уникальной части контекста.

Документация vLLM для параметров scheduler и chunked prefill:
- https://docs.vllm.ai/en/v0.33.0/api/vllm/config/
- https://docs.vllm.ai/en/v0.33.0/configuration/conserving_memory/

## 11. Диагностика

```powershell
kubectl get pods -n llm
kubectl get deployment,svc,pvc -n llm
kubectl describe pod -n llm -l app=vllm-api
kubectl logs deployment/vllm-api -n llm -c model-downloader --tail=100
kubectl logs deployment/vllm-api -n llm -c vllm --tail=100
```

Перезапуск Deployment без удаления PVC:

```powershell
kubectl rollout restart deployment/vllm-api -n llm
kubectl rollout status deployment/vllm-api -n llm --timeout=10m
```

При исправном кэше initContainer должен сообщить, что модель уже доступна и повторное скачивание пропускается.

## 12. Ограничения и дальнейшая работа

- Инференс выполняется только на CPU и поэтому заметно медленнее GPU-сценариев.
- Используется небольшая модель 0.5B и `max_model_len: 1024`, что ограничивает доступный контекст.
- Локальная выборка benchmark содержит по четыре измерения на сценарий; p95 пока предварительный.
- Увеличение `max_num_seqs`, `max_num_batched_tokens`, контекста или KV cache требует повторного тестирования на том же стенде и контроля RAM/CPU.
- `python:3.12-slim-bookworm` в Dockerfile скачивателя указан тегом; для ещё более строгой воспроизводимости инфраструктуры можно дополнительно закрепить базовые образы по digest.



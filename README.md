# conda-cdn-log

An opt-in conda plugin for collecting HTTP diagnostics in CI. It records HTTP
responses, including failures that recover on retry, as JSON Lines. The
`conda cdn-log` subcommand produces a Markdown summary for the GitHub Actions
run summary. GitHub Actions uploads the detailed records as an artifact.

The plugin writes local files only. It does not send diagnostics to a service,
change conda's retry settings, or suppress a failed conda command.

## Try it locally

Use Python 3.11 or later in an environment containing conda 26.7.3 or later,
Requests 2, and urllib3 2. Install the plugin into the environment whose Python runs conda.
Installing it into an environment that conda is creating will not enable it in
the running conda process.

From the root of this repository, with that Python on `PATH`:

```sh
python -m pip install --no-deps .
export CONDA_CDN_LOG_DIR="$PWD/cdn-events"
python -m conda search --override-channels --channel conda-forge zlib
python -m conda cdn-log "$CONDA_CDN_LOG_DIR"
```

`--no-deps` keeps this install from changing the environment's HTTP libraries.
Conda must already be installed. The plugin uses an internal urllib3 method, so
test compatibility before changing the HTTP library versions used in CI.

If the command contacts an eligible host, the output directory contains one or
more `.jsonl` files. A command served entirely from cache can produce no records.
An empty summary therefore reports that no observations were recorded.

## Use it in GitHub Actions

See [the workflow example](examples/github-actions.yml). Its collection steps
can also be added to an existing conda CI job after conda has been provisioned.
Install the plugin before the commands being measured and set
`CONDA_CDN_LOG_DIR` for those commands.

This repository's [CI workflow](.github/workflows/ci.yml) uses the plugin on
Linux, macOS, and Windows. After the checks, each job runs a live conda search
with an empty package cache and verifies that it captured HTTP responses. The
job appends the summary and uploads its records even if an earlier step fails.
Requests made while Pixi installs the development environment are not captured.

The example selects base's Python explicitly with `conda run -n base`. In a
test job that runs conda from another Python environment, install and invoke the
plugin there instead. For matrix jobs, add the matrix values to the artifact
name so parallel jobs do not try to upload the same artifact.

Keep the conda operation, summary generation, and artifact upload in separate
steps. Both reporting steps use `if: always()` so a failed conda operation still
produces diagnostics. The original operation retains its failure status.

The summary command writes aggregate tables to stdout. With
`--github-summary`, it also appends those tables to the file named by
`GITHUB_STEP_SUMMARY`:

```sh
python -m conda cdn-log "$CONDA_CDN_LOG_DIR" --github-summary
```

Headers appear only in the JSONL artifact. They are not printed in the run
summary. The example retains artifacts for 14 days. Artifact visibility follows
the repository and workflow permissions, so choose hosts and retention for the
CI job receiving the data.

## Read the results

The first table contains HTTP response counts, 4xx counts, 5xx counts, the 5xx
response rate, and terminal transport failures for each host. A second table
breaks down error responses by status code.

A `502 → 200` sequence contributes two responses and one 5xx response, giving a
50% 5xx response rate. It represents a request that recovered. Terminal
transport failures, such as a connection failure or timeout after retries, have
no HTTP status and are counted separately. Intermediate connection failures
that recover are not counted.

Conda can receive expected 404s while trying alternative metadata files. An
HTTP error response does not establish whether a CDN, origin server, proxy, or
request caused it. Compare the UTC timestamps and selected diagnostic headers
when investigating an incident.

To combine downloaded artifacts, pass their directories or individual files:

```sh
python -m conda cdn-log downloaded-artifacts
python -m conda cdn-log first.jsonl second.jsonl
```

Directories are searched recursively for `.jsonl` files. Missing inputs and
malformed records, including a partial final line left by a killed process, are
reported in the summary. Rates then cover only the readable records.

## Configuration

| Variable | Default | Behavior |
| --- | --- | --- |
| `CONDA_CDN_LOG_DIR` | Unset | Capture is disabled unless this is a nonempty directory path. |
| `CONDA_CDN_LOG_HOSTS` | `repo.anaconda.com,conda.anaconda.org` | Comma-separated hostnames whose requests are eligible for capture. |
| `GITHUB_STEP_SUMMARY` | Set by GitHub Actions | Destination used only when the summary command receives `--github-summary`. |

Host matching compares complete hostnames, ignoring case and a trailing dot.
Whitespace around configured hostnames is ignored. A subdomain or lookalike
such as `repo.anaconda.com.example.org` does not match `repo.anaconda.com`.
Redirect destinations are checked against the same list. To add a host, include
the defaults as well if they should remain eligible:

```sh
export CONDA_CDN_LOG_HOSTS="repo.anaconda.com,conda.anaconda.org,packages.example.org"
```

Configuration is read when the first supported conda command enables capture.
Repeated commands in the same Python process reuse that configuration. Start a
new process to change it. Unset `CONDA_CDN_LOG_DIR` to disable collection in new
processes. `CONDA_NO_PLUGINS=true` also prevents conda from loading the plugin.

## Recorded data

Each line is a JSON object with `schema_version: 1`. Common fields are:

| Field | Meaning |
| --- | --- |
| `timestamp` | UTC timestamp when the event was recorded, including an offset. |
| `event` | `response` or `transport_error`. |
| `host` | Eligible request hostname. |
| `method` | HTTP request method. |
| `request_id` | Random identifier shared by responses from one adapter send, including its retries. |
| `duration_ms` | Time spent in that response attempt, or in the adapter send for a terminal transport failure. |

A `response` event includes `status`. Responses with status 400 or higher also
include `request_headers` and `response_headers`. Successful responses omit
headers. A `transport_error` event includes `request_headers` and `error_type`,
classified as `ConnectionError`, `Timeout`, or `SSLError`. It omits exception
messages, which can contain sensitive URLs.

The request header allowlist is `Accept`, `Range`, and `If-Modified-Since`.
The response header allowlist is `Date`, `Server`, `Age`, `Retry-After`,
`Content-Type`, `Content-Length`, `Content-Range`, `CF-Ray`, `CF-Cache-Status`,
`X-Cache`, `X-Cache-Hits`, `X-Amz-Cf-Id`, `X-Amz-Cf-Pop`, `X-Served-By`, and
`X-Timer`. Stored names are lowercase and values are limited to 512 characters.

The logger omits URL paths, query strings, user information, request and
response bodies, authorization, cookies, redirect locations, and all headers
outside those lists. This is data minimization, not inspection of arbitrary
header contents. A server can put unexpected data in an allowed header.

Files have distinct names per process and append records under a thread lock.
On POSIX systems, new files are created with mode `0600`, subject to the process
umask. A write failure disables that process's recorder and emits one generic
warning without a path. The conda request continues with its original result.

## Coverage and compatibility

Capture starts through conda's pre-command plugin hook. It covers supported
create, install, update, remove, search, list, info, notices, and environment
create/update commands. Direct Python use of conda's networking before such a
command runs is not observed. Plugin discovery itself does not patch networking
or write files.

The implementation observes conda's Requests HTTP adapter and
`urllib3.HTTPConnectionPool._make_request`. The latter is private API. The
declared HTTP dependencies are Requests 2 and urllib3 2. Retest capture when
upgrading those dependencies or conda.

Only eligible Requests traffic is included. Native transport replacements,
shard providers that bypass Requests, separate programs such as `mamba` or
`pixi`, and failures while reading a response body may be unobserved. Solver
selection alone does not determine coverage. A 0% rate means the recorded
responses contained no 5xx status, not that all network activity succeeded.

The plugin is installed from source. Install it into the CI environment before
running the commands whose requests you want to capture.

## Develop the plugin

The Pixi workspace selects Python 3.12, conda `>=26.7.3,<27`, Requests 2, and
urllib3 2. From this directory:

```sh
pixi run test
pixi run lint
pixi run format-check
pixi run type-check
```

The tests use local HTTP responses to exercise retries and redaction. They do
not require inducing failures on a public CDN. A passing local test run does
not establish that a hosted workflow has run or that every declared platform
has been tested.

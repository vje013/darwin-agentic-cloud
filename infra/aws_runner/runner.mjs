// darwin-runner Lambda function (Node.js).
//
// Deployed as a container image to AWS Lambda in each supported region:
//     darwin-runner-node-{region}
//
// Mirrors infra/aws_runner/runner.py in protocol: takes a RunnerEvent,
// shells out the workload to `node`, captures stdout/stderr/exit_code,
// returns a RunnerResponse. Same schema URI:
//     darwin.cloud/event/aws-lambda-runner/v1
//
// Tests for the schema this consumes/produces live with the substrate
// adapter (tests/substrate/test_aws_lambda.py). The two handlers MUST
// stay protocol-compatible.

import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { createHash } from "node:crypto";

const EVENT_SCHEMA_URI = "darwin.cloud/event/aws-lambda-runner/v1";
const SUPPORTED_LANGUAGES = new Set(["node"]);
const MAX_CODE_BYTES = 192 * 1024;
const MIN_MEMORY_MB = 128;
const MAX_MEMORY_MB = 10240;
const MAX_TIMEOUT_SEC = 900;

class RunnerEventError extends Error {}

function validateEvent(event) {
  if (typeof event !== "object" || event === null || Array.isArray(event)) {
    throw new RunnerEventError("event must be a JSON object");
  }
  const required = [
    "schema", "request_id", "workload_id", "language",
    "code", "timeout_sec", "memory_mb",
  ];
  const missing = required.filter((k) => !(k in event));
  if (missing.length > 0) {
    throw new RunnerEventError(`event missing fields: ${JSON.stringify(missing)}`);
  }
  if (event.schema !== EVENT_SCHEMA_URI) {
    throw new RunnerEventError(
      `event schema mismatch: got ${JSON.stringify(event.schema)}, ` +
      `expected ${JSON.stringify(EVENT_SCHEMA_URI)}`
    );
  }
  if (!SUPPORTED_LANGUAGES.has(event.language)) {
    throw new RunnerEventError(
      `event language not supported by node runner: ${JSON.stringify(event.language)}`
    );
  }
  if (typeof event.code !== "string" || event.code.length === 0) {
    throw new RunnerEventError("event.code must be a non-empty string");
  }
  if (Buffer.byteLength(event.code, "utf-8") > MAX_CODE_BYTES) {
    throw new RunnerEventError(`event.code exceeds ${MAX_CODE_BYTES} bytes`);
  }
  if (!Number.isInteger(event.timeout_sec) || event.timeout_sec < 1 || event.timeout_sec > MAX_TIMEOUT_SEC) {
    throw new RunnerEventError(
      `event.timeout_sec must be 1..${MAX_TIMEOUT_SEC}, got ${event.timeout_sec}`
    );
  }
  if (!Number.isInteger(event.memory_mb) || event.memory_mb < MIN_MEMORY_MB || event.memory_mb > MAX_MEMORY_MB) {
    throw new RunnerEventError(
      `event.memory_mb must be ${MIN_MEMORY_MB}..${MAX_MEMORY_MB}, got ${event.memory_mb}`
    );
  }
}

function sha256Hex(s) {
  return createHash("sha256").update(s, "utf-8").digest("hex");
}

function executeWorkload({ code, timeoutSec }) {
  const filePath = "/tmp/workload.js";
  writeFileSync(filePath, code, "utf-8");

  const startedAt = Date.now() / 1000;
  let result;
  try {
    result = spawnSync("node", [filePath], {
      encoding: "utf-8",
      timeout: timeoutSec * 1000,
      maxBuffer: 10 * 1024 * 1024,
    });
  } catch (e) {
    const endedAt = Date.now() / 1000;
    return {
      status: "error",
      stdout: "",
      stderr: String(e),
      exit_code: null,
      started_at: startedAt,
      ended_at: endedAt,
      wall_time_sec: endedAt - startedAt,
      output_hash: sha256Hex(""),
      stderr_hash: sha256Hex(String(e)),
      error: `${e.name || "Error"}: ${e.message || e}`,
    };
  }
  const endedAt = Date.now() / 1000;

  // spawnSync uses signal === "SIGTERM" when timeout fires.
  if (result.signal === "SIGTERM") {
    return {
      status: "timeout",
      stdout: result.stdout || "",
      stderr: result.stderr || "",
      exit_code: null,
      started_at: startedAt,
      ended_at: endedAt,
      wall_time_sec: endedAt - startedAt,
      output_hash: sha256Hex(result.stdout || ""),
      stderr_hash: sha256Hex(result.stderr || ""),
      error: `timeout after ${timeoutSec}s`,
    };
  }

  const status = result.status === 0 ? "ok" : "error";
  return {
    status,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    exit_code: result.status,
    started_at: startedAt,
    ended_at: endedAt,
    wall_time_sec: endedAt - startedAt,
    output_hash: sha256Hex(result.stdout || ""),
    stderr_hash: sha256Hex(result.stderr || ""),
    error: null,
  };
}

export const handler = async (event, _context) => {
  validateEvent(event);

  const effectiveTimeout = Math.min(event.timeout_sec, MAX_TIMEOUT_SEC - 5);
  const r = executeWorkload({ code: event.code, timeoutSec: effectiveTimeout });

  const response = {
    schema: EVENT_SCHEMA_URI,
    request_id: event.request_id,
    workload_id: event.workload_id,
    status: r.status,
    stdout: r.stdout,
    stderr: r.stderr,
    exit_code: r.exit_code,
    started_at: r.started_at,
    ended_at: r.ended_at,
    wall_time_sec: r.wall_time_sec,
    output_hash: r.output_hash,
    stderr_hash: r.stderr_hash,
  };
  if (r.error !== null) {
    response.error = r.error;
  }
  return response;
};

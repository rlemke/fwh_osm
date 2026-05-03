#!/usr/bin/env bash
# Shared helper for the all-*.sh scripts in this directory.
#
# Provides:
#   CONTINENTS — bash array of every top-level Geofabrik slug, in the
#                order the all.sh batch runs them.
#   run_per_continent <tool.sh> [tool-args…] — iterates CONTINENTS,
#                invokes "<tool.sh> --all-under <continent>
#                --include-parents [tool-args…]" per iteration,
#                absorbs per-continent failures into a summary, and
#                prints a coloured [ok]/[fail]/===header=== log
#                matching update-all.sh's idiom.
#
# Source (not exec): this file intentionally has no shebang-main and
# is not chmod +x'd. Every caller is a tiny all-<tool>.sh that
# sources this file and then calls run_per_continent.

# Geofabrik's nine top-level region slugs. Russia and antarctica are
# top-level rather than nested under Europe/Asia. australia-oceania
# covers Australia + NZ + Pacific islands.
CONTINENTS=(
    north-america
    central-america
    south-america
    europe
    asia
    africa
    australia-oceania
    russia
    antarctica
)

# Colour helpers — no-ops when stdout isn't a terminal so log-
# redirect runs stay grep-friendly.
if [ -t 1 ]; then
    __AFL_BOLD=$'\033[1m'
    __AFL_GREEN=$'\033[32m'
    __AFL_RED=$'\033[31m'
    __AFL_RESET=$'\033[0m'
else
    __AFL_BOLD="" __AFL_GREEN="" __AFL_RED="" __AFL_RESET=""
fi

run_per_continent() {
    # First positional is the tool script path; remaining positionals
    # are forwarded verbatim to every invocation. Use: call with "$@"
    # intact so bash arrays and dashes survive the boundary.
    local tool="$1"; shift
    if [ -z "${tool:-}" ] || [ ! -x "$tool" ]; then
        printf '%serror%s: run_per_continent: tool %q not executable\n' \
            "$__AFL_RED" "$__AFL_RESET" "$tool" >&2
        return 2
    fi

    local tool_name
    tool_name="$(basename "$tool" .sh)"

    local failed=()
    local total=${#CONTINENTS[@]}
    local idx=0
    local start_epoch
    start_epoch=$(date +%s)

    local continent
    for continent in "${CONTINENTS[@]}"; do
        idx=$((idx + 1))
        printf '\n%s=== [%d/%d] %s %s ===%s\n' \
            "$__AFL_BOLD" "$idx" "$total" \
            "$tool_name" "$continent" "$__AFL_RESET"
        if "$tool" --all-under "$continent" --include-parents "$@"; then
            printf '%s[ok]%s %s %s\n' \
                "$__AFL_GREEN" "$__AFL_RESET" "$tool_name" "$continent"
        else
            local rc=$?
            printf '%s[fail]%s %s %s (exit %d) — moving on\n' \
                "$__AFL_RED" "$__AFL_RESET" "$tool_name" "$continent" "$rc"
            failed+=("$continent")
        fi
    done

    local elapsed=$(( $(date +%s) - start_epoch ))
    echo
    printf '%s=== %s summary ===%s\n' "$__AFL_BOLD" "$tool_name" "$__AFL_RESET"
    printf 'continents: %d ok, %d failed (%s)  ·  elapsed: %ds\n' \
        "$((total - ${#failed[@]}))" \
        "${#failed[@]}" \
        "${failed[*]:-none}" \
        "$elapsed"

    [ ${#failed[@]} -eq 0 ]
}

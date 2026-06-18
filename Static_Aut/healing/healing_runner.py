from Static_Aut.execution.web_executors import execute_static_web_flow
from Static_Aut.healing.self_healer import (
    apply_locator_patch,
    apply_runtime_locator_patch,
    heal_static_locator_with_mcp,
)
from Static_Aut.toolbox.static_toolbox import StaticExecutionResult, StaticExecutionStatus


async def run_static_locator_healing(
    test_key: str,
    scenario_text: str,
    static_result: StaticExecutionResult,
    logger,
) -> StaticExecutionResult:
    attempted_locator_keys: set[str] = set()
    accepted_patches = []
    while (
        static_result.status == StaticExecutionStatus.FAILED
        and static_result.failed_locator_key
        and static_result.failed_locator_key not in attempted_locator_keys
    ):
        previous_failed_locator_key = static_result.failed_locator_key
        attempted_locator_keys.add(previous_failed_locator_key)
        logger.info(
            "Step 1.5: Starting MCP locator discovery for locator_key=%s...",
            previous_failed_locator_key,
        )
        static_result.healing_attempted = True
        try:
            healing_result = await heal_static_locator_with_mcp(
                scenario_text,
                static_result,
            )
            if not healing_result:
                static_result.healing_patch = {
                    "locator_key": previous_failed_locator_key,
                    "error": "MCP locator discovery did not produce a locator patch.",
                }
                logger.warning("MCP locator discovery did not produce a locator patch.")
                break

            healing_patch = healing_result.patch
            token_usage = healing_result.token_usage
            logger.info(
                "MCP locator discovery token usage | prompt_tokens=%s | completion_tokens=%s | total_tokens=%s",
                token_usage.prompt_tokens,
                token_usage.completion_tokens,
                token_usage.total_tokens,
            )
            apply_runtime_locator_patch(healing_patch)
            accepted_patches.append(healing_patch)
            static_result = await execute_static_web_flow(
                test_key,
                scenario_text,
                tool_overrides=static_result.tool_overrides,
            )
            static_result.healing_attempted = True
            static_result.healing_patch = {
                "app_name": healing_patch.app_name,
                "locator_key": healing_patch.locator_key,
                "selector": healing_patch.selector,
                "reason": healing_patch.reason,
            }
            logger.info(
                "Static toolbox retry after MCP locator repair | status=%s | summary=%s",
                static_result.status.value,
                static_result.summary,
            )

            if (
                static_result.status == StaticExecutionStatus.FAILED
                and (
                    getattr(static_result, "failure_from_expected_validation", False)
                    or static_result.failed_tool == "expected_result_validation"
                )
            ):
                logger.info(
                    "Stopping MCP locator healing because the retry failed on an Expected Result assertion.",
                )
                _persist_healing_patches(accepted_patches, logger)
                break

            if static_result.status == StaticExecutionStatus.PASSED:
                _persist_healing_patches(accepted_patches, logger)
                break

            if (
                static_result.status == StaticExecutionStatus.FAILED
                and static_result.failed_locator_key == previous_failed_locator_key
            ):
                logger.info(
                    "Static retry failed on the same locator_key=%s after MCP repair.",
                    static_result.failed_locator_key,
                )
                break
        except Exception as exc:
            static_result.healing_patch = {
                "locator_key": previous_failed_locator_key,
                "error": str(exc),
            }
            logger.exception(f"MCP locator discovery failed before static retry: {exc}")
            break
    return static_result


def should_run_static_locator_healing(static_result: StaticExecutionResult, static_mode: str, enabled: bool) -> bool:
    # Locator healing is only allowed during real static execution, not shadow/off mode.
    # It is also gated by the STATIC_SELF_HEALING_ENABLED config value.
    return (
        static_mode == "on"
        and enabled
        # Healing only makes sense after a failed static run.
        and static_result.status == StaticExecutionStatus.FAILED
        # getattr(..., False) safely reads the optional flag. If older/static results
        # do not have failure_from_expected_validation, it behaves as False.
        and not getattr(static_result, "failure_from_expected_validation", False)
        # Expected Result failures are assertion failures, not locator discovery gaps.
        and static_result.failed_tool != "expected_result_validation"
    )


def _persist_healing_patches(healing_patches, logger) -> None:
    for healing_patch in healing_patches:
        try:
            apply_locator_patch(healing_patch)
            logger.info(
                "MCP locator discovery applied patch after successful retry | locator_key=%s | selector=%s | reason=%s",
                healing_patch.locator_key,
                healing_patch.selector,
                healing_patch.reason,
            )
        except Exception as exc:
            logger.exception(
                "Failed to persist healed locator patch for locator_key=%s: %s",
                healing_patch.locator_key,
                exc,
            )

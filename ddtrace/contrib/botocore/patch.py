"""
Trace queries to aws api done via botocore client
"""
import collections
import os
from typing import List  # noqa:F401
from typing import Set  # noqa:F401
from typing import Union  # noqa:F401

from botocore import __version__
import botocore.client
import botocore.exceptions

from ddtrace import config
from ddtrace.contrib.trace_utils import with_traced_module
from ddtrace.internal.agent import get_stats_url
from ddtrace.internal.llmobs.integrations import BedrockIntegration
from ddtrace.internal.schema.span_attribute_schema import SpanDirection
from ddtrace.settings.config import Config
from ddtrace.vendor import wrapt

from ...constants import SPAN_KIND
from ...ext import SpanKind
from ...ext import SpanTypes
from ...ext import http
from ...internal.constants import COMPONENT
from ...internal.logger import get_logger
from ...internal.schema import schematize_cloud_api_operation
from ...internal.schema import schematize_cloud_faas_operation
from ...internal.schema import schematize_cloud_messaging_operation
from ...internal.schema import schematize_service_name
from ...internal.utils import get_argument_value
from ...internal.utils.formats import asbool
from ...internal.utils.formats import deep_getattr
from ...pin import Pin
from ..trace_utils import unwrap
from .services.bedrock import patched_bedrock_api_call
from .services.kinesis import patched_kinesis_api_call
from .services.sqs import inject_trace_to_sqs_or_sns_batch_message
from .services.sqs import inject_trace_to_sqs_or_sns_message
from .services.sqs import patched_sqs_api_call
from .services.stepfunctions import inject_trace_to_stepfunction_input
from .services.stepfunctions import patched_stepfunction_api_call
from .utils import inject_trace_to_client_context
from .utils import inject_trace_to_eventbridge_detail
from .utils import set_patched_api_call_span_tags
from .utils import set_response_metadata_tags


_PATCHED_SUBMODULES = set()  # type: Set[str]

# Original botocore client class
_Botocore_client = botocore.client.BaseClient

ARGS_NAME = ("action", "params", "path", "verb")
TRACED_ARGS = {"params", "path", "verb"}

log = get_logger(__name__)


# Botocore default settings
config._add(
    "botocore",
    {
        "distributed_tracing": asbool(os.getenv("DD_BOTOCORE_DISTRIBUTED_TRACING", default=True)),
        "invoke_with_legacy_context": asbool(os.getenv("DD_BOTOCORE_INVOKE_WITH_LEGACY_CONTEXT", default=False)),
        "llmobs_enabled": asbool(os.getenv("DD_BEDROCK_LLMOBS_ENABLED", False)),
        "operations": collections.defaultdict(Config._HTTPServerConfig),
        "span_prompt_completion_sample_rate": float(os.getenv("DD_BEDROCK_SPAN_PROMPT_COMPLETION_SAMPLE_RATE", 1.0)),
        "llmobs_prompt_completion_sample_rate": float(
            os.getenv("DD_LANGCHAIN_LLMOBS_PROMPT_COMPLETION_SAMPLE_RATE", 1.0)
        ),
        "span_char_limit": int(os.getenv("DD_BEDROCK_SPAN_CHAR_LIMIT", 128)),
        "tag_no_params": asbool(os.getenv("DD_AWS_TAG_NO_PARAMS", default=False)),
        "instrument_internals": asbool(os.getenv("DD_BOTOCORE_INSTRUMENT_INTERNALS", default=False)),
        "_api_key": os.getenv("DD_API_KEY"),
        "_app_key": os.getenv("DD_APP_KEY"),
        "propagation_enabled": asbool(os.getenv("DD_BOTOCORE_PROPAGATION_ENABLED", default=False)),
        "empty_poll_enabled": asbool(os.getenv("DD_BOTOCORE_EMPTY_POLL_ENABLED", default=True)),
    },
)


def get_version():
    # type: () -> str
    return __version__


def patch():
    if getattr(botocore.client, "_datadog_patch", False):
        return
    botocore.client._datadog_patch = True

    botocore._datadog_integration = BedrockIntegration(config=config.botocore, stats_url=get_stats_url())
    wrapt.wrap_function_wrapper("botocore.client", "BaseClient._make_api_call", patched_api_call(botocore))
    Pin(service="aws").onto(botocore.client.BaseClient)
    wrapt.wrap_function_wrapper("botocore.parsers", "ResponseParser.parse", patched_lib_fn)
    Pin(service="aws").onto(botocore.parsers.ResponseParser)
    _PATCHED_SUBMODULES.clear()


def unpatch():
    _PATCHED_SUBMODULES.clear()
    if getattr(botocore.client, "_datadog_patch", False):
        botocore.client._datadog_patch = False
        unwrap(botocore.parsers.ResponseParser, "parse")
        unwrap(botocore.client.BaseClient, "_make_api_call")


def patch_submodules(submodules):
    # type: (Union[List[str], bool]) -> None
    if isinstance(submodules, bool) and submodules:
        _PATCHED_SUBMODULES.clear()
    elif isinstance(submodules, list):
        submodules = [sub_module.lower() for sub_module in submodules]
        _PATCHED_SUBMODULES.update(submodules)


def patched_lib_fn(original_func, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled() or not config.botocore["instrument_internals"]:
        return original_func(*args, **kwargs)
    with pin.tracer.trace("{}.{}".format(original_func.__module__, original_func.__name__)) as span:
        span.set_tag_str(COMPONENT, config.botocore.integration_name)
        span.set_tag_str(SPAN_KIND, SpanKind.CLIENT)
        return original_func(*args, **kwargs)


@with_traced_module
def patched_api_call(botocore, pin, original_func, instance, args, kwargs):
    if not pin or not pin.enabled():
        return original_func(*args, **kwargs)

    endpoint_name = deep_getattr(instance, "_endpoint._endpoint_prefix")

    if _PATCHED_SUBMODULES and endpoint_name not in _PATCHED_SUBMODULES:
        return original_func(*args, **kwargs)

    trace_operation = schematize_cloud_api_operation(
        "{}.command".format(endpoint_name), cloud_provider="aws", cloud_service=endpoint_name
    )

    operation = get_argument_value(args, kwargs, 0, "operation_name", True)
    params = get_argument_value(args, kwargs, 1, "api_params", True)

    function_vars = {
        "endpoint_name": endpoint_name,
        "operation": operation,
        "params": params,
        "pin": pin,
        "trace_operation": trace_operation,
        "integration": botocore._datadog_integration,
    }

    if endpoint_name == "kinesis":
        return patched_kinesis_api_call(
            original_func=original_func,
            instance=instance,
            args=args,
            kwargs=kwargs,
            function_vars=function_vars,
        )
    elif endpoint_name == "sqs":
        return patched_sqs_api_call(
            original_func=original_func,
            instance=instance,
            args=args,
            kwargs=kwargs,
            function_vars=function_vars,
        )
    elif endpoint_name == "bedrock-runtime" and operation.startswith("InvokeModel"):
        return patched_bedrock_api_call(
            original_func=original_func,
            instance=instance,
            args=args,
            kwargs=kwargs,
            function_vars=function_vars,
        )
    elif endpoint_name == "states":
        return patched_stepfunction_api_call(
            original_func=original_func, instance=instance, args=args, kwargs=kwargs, function_vars=function_vars
        )
    else:
        # this is the default patched api call
        return patched_api_call_fallback(
            original_func=original_func,
            instance=instance,
            args=args,
            kwargs=kwargs,
            function_vars=function_vars,
        )


def patched_api_call_fallback(original_func, instance, args, kwargs, function_vars):
    # default patched api call that is used generally for several services / operations
    params = function_vars.get("params")
    trace_operation = function_vars.get("trace_operation")
    pin = function_vars.get("pin")
    endpoint_name = function_vars.get("endpoint_name")
    operation = function_vars.get("operation")

    with pin.tracer.trace(
        trace_operation,
        service=schematize_service_name("{}.{}".format(pin.service, endpoint_name)),
        span_type=SpanTypes.HTTP,
    ) as span:
        set_patched_api_call_span_tags(span, instance, args, params, endpoint_name, operation)

        if args:
            if config.botocore["distributed_tracing"]:
                try:
                    if endpoint_name == "lambda" and operation == "Invoke":
                        inject_trace_to_client_context(params, span)
                        span.name = schematize_cloud_faas_operation(
                            trace_operation, cloud_provider="aws", cloud_service="lambda"
                        )
                    if endpoint_name == "events" and operation == "PutEvents":
                        inject_trace_to_eventbridge_detail(params, span)
                        span.name = schematize_cloud_messaging_operation(
                            trace_operation,
                            cloud_provider="aws",
                            cloud_service="events",
                            direction=SpanDirection.OUTBOUND,
                        )
                    if endpoint_name == "sns" and operation == "Publish":
                        inject_trace_to_sqs_or_sns_message(
                            params,
                            span,
                            endpoint_service=endpoint_name,
                        )
                        span.name = schematize_cloud_messaging_operation(
                            trace_operation,
                            cloud_provider="aws",
                            cloud_service="sns",
                            direction=SpanDirection.OUTBOUND,
                        )
                    if endpoint_name == "sns" and operation == "PublishBatch":
                        inject_trace_to_sqs_or_sns_batch_message(
                            params,
                            span,
                            endpoint_service=endpoint_name,
                        )
                        span.name = schematize_cloud_messaging_operation(
                            trace_operation,
                            cloud_provider="aws",
                            cloud_service="sns",
                            direction=SpanDirection.OUTBOUND,
                        )
                    if endpoint_name == "states" and (
                        operation == "StartExecution" or operation == "StartSyncExecution"
                    ):
                        inject_trace_to_stepfunction_input(params, span)
                        span.name = schematize_cloud_messaging_operation(
                            trace_operation,
                            cloud_provider="aws",
                            cloud_service="stepfunctions",
                            direction=SpanDirection.OUTBOUND,
                        )
                except Exception:
                    log.warning("Unable to inject trace context", exc_info=True)

        try:
            result = original_func(*args, **kwargs)
            set_response_metadata_tags(span, result)
            return result

        except botocore.exceptions.ClientError as e:
            # `ClientError.response` contains the result, so we can still grab response metadata
            set_response_metadata_tags(span, e.response)

            # If we have a status code, and the status code is not an error,
            #   then ignore the exception being raised
            status_code = span.get_tag(http.STATUS_CODE)
            if status_code and not config.botocore.operations[span.resource].is_error_code(int(status_code)):
                span._ignore_exception(botocore.exceptions.ClientError)
            raise

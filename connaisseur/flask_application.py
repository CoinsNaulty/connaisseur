import asyncio
import os
import traceback
import logging
from flask import Flask, request, jsonify
from connaisseur.exceptions import (
    BaseConnaisseurException,
    AlertSendingError,
    ConfigurationError,
)
from connaisseur.util import get_admission_review
from connaisseur.alert import send_alerts
from connaisseur.config import Config
from connaisseur.admission_request import AdmissionRequest


DETECTION_MODE = os.environ.get("DETECTION_MODE", "0") == "1"

APP = Flask(__name__)
CONFIG = Config()
"""
Flask Server that admits the request send to the k8s cluster, validates it and
sends its response back.
"""


@APP.errorhandler(AlertSendingError)
def handle_alert_sending_failure(err):
    logging.error(err.message)
    return "Alert could not be sent. Check the logs for more details!", 500


@APP.errorhandler(ConfigurationError)
def handle_alert_config_error(err):
    logging.error(err.message)
    return (
        "Alerting configuration is not valid. Check the logs for more details!",
        500,
    )


@APP.route("/mutate", methods=["POST"])
def mutate():
    """
    Handles the '/mutate' path and accepts CREATE and UPDATE requests.
    Sends its response back, which either denies or allows the request.
    """
    admission_request = None
    try:
        logging.debug(request.json)
        admission_request = AdmissionRequest(request.json)
        response = asyncio.run(__admit(admission_request))
    except Exception as err:
        if isinstance(err, BaseConnaisseurException):
            err_log = str(err)
            msg = err.user_msg  # pylint: disable=no-member
        else:
            err_log = str(traceback.format_exc())
            msg = "unknown error. please check the logs."
        send_alerts(admission_request, False, msg)
        logging.error(err_log)
        uid = admission_request.uid if admission_request else ""
        return jsonify(
            get_admission_review(
                uid,
                False,
                msg=msg,
                detection_mode=DETECTION_MODE,
            )
        )

    send_alerts(admission_request, True)
    return jsonify(response)


# health probe
@APP.route("/health", methods=["GET", "POST"])
def healthz():
    """
    Handles the '/health' endpoint and checks the health status of the flask
    server. Sends back '200'.
    """

    return "", 200


# readiness probe
@APP.route("/ready", methods=["GET", "POST"])
def readyz():
    return "", 200


def __create_logging_msg(msg: str, **kwargs):
    return str({"message": msg, "context": dict(**kwargs)})


async def __admit(admission_request: AdmissionRequest):
    logging_context = dict(admission_request.context)
    patches = []

    patches = asyncio.gather(
        *[
            __validate_image(type_and_index, image, admission_request)
            for type_and_index, image in admission_request.wl_object.containers.items()
        ]
    )

    try:
        await patches
    except BaseConnaisseurException as err:
        err.update_context(**logging_context)
        raise err

    return get_admission_review(
        admission_request.uid,
        True,
        patch=[patch for patch in patches.result() if patch],
    )


async def __validate_image(type_index, image, admission_request):
    logging_context = dict(admission_request.context)
    original_image = str(image)
    type_, index = type_index
    logging_context.update(image=original_image)

    # child resources have mutated image names, as their parents got mutated
    # before their creation. this may result in mismatch of rules or duplicate
    # lookups for already approved images. so child resources are automatically
    # approved without further check ups, if their parents were approved
    # earlier.
    child_approval_on = os.environ.get("AUTOMATIC_CHILD_APPROVAL_ENABLED", "1") == "1"

    if child_approval_on & (
        image in admission_request.wl_object.parent_containers.values()
    ):
        msg = f'automatic child approval for "{original_image}".'
        logging.info(__create_logging_msg(msg, **logging_context))
        return

    try:
        policy_rule = CONFIG.get_policy_rule(image)
        validator = CONFIG.get_validator(policy_rule.validator)

        msg = (
            f'starting verification of image "{original_image}" using rule '
            f'"{str(policy_rule)}" with arguments {str(policy_rule.arguments)}'
            f' and validator "{str(validator)}".'
        )
        logging.debug(
            __create_logging_msg(
                msg, **logging_context, policy_rule=policy_rule, validator=validator
            )
        )

        trusted_digest = await validator.validate(image, **policy_rule.arguments)
    except BaseConnaisseurException as err:
        raise err
    msg = f'successful verification of image "{original_image}"'
    logging.info(__create_logging_msg(msg, **logging_context))
    if trusted_digest:
        image.set_digest(trusted_digest)
        return admission_request.wl_object.get_json_patch(image, type_, index)
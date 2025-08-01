#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import json
import re
import time

import tiktoken
from flask import Response, jsonify, request

from agent.canvas import Canvas
from api.db import LLMType, StatusEnum
from api.db.db_models import APIToken
from api.db.services.api_service import API4ConversationService
from api.db.services.canvas_service import UserCanvasService, completionOpenAI
from api.db.services.canvas_service import completion as agent_completion
from api.db.services.conversation_service import ConversationService, iframe_completion
from api.db.services.conversation_service import completion as rag_completion
from api.db.services.dialog_service import DialogService, ask, chat
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.utils import get_uuid
from api.utils.api_utils import check_duplicate_ids, get_data_openai, get_error_data_result, get_result, token_required, validate_request
from rag.prompts import chunks_format


@manager.route("/chats/<chat_id>/sessions", methods=["POST"])  # noqa: F821
@token_required
def create(tenant_id, chat_id):
    req = request.json
    req["dialog_id"] = chat_id
    dia = DialogService.query(tenant_id=tenant_id, id=req["dialog_id"], status=StatusEnum.VALID.value)
    if not dia:
        return get_error_data_result(message="You do not own the assistant.")
    conv = {
        "id": get_uuid(),
        "dialog_id": req["dialog_id"],
        "name": req.get("name", "New session"),
        "message": [{"role": "assistant", "content": dia[0].prompt_config.get("prologue")}],
        "user_id": req.get("user_id", ""),
    }
    if not conv.get("name"):
        return get_error_data_result(message="`name` can not be empty.")
    ConversationService.save(**conv)
    e, conv = ConversationService.get_by_id(conv["id"])
    if not e:
        return get_error_data_result(message="Fail to create a session!")
    conv = conv.to_dict()
    conv["messages"] = conv.pop("message")
    conv["chat_id"] = conv.pop("dialog_id")
    del conv["reference"]
    return get_result(data=conv)


@manager.route("/agents/<agent_id>/sessions", methods=["POST"])  # noqa: F821
@token_required
def create_agent_session(tenant_id, agent_id):
    req = request.json
    if not request.is_json:
        req = request.form
    files = request.files
    user_id = request.args.get("user_id", "")
    e, cvs = UserCanvasService.get_by_id(agent_id)
    if not e:
        return get_error_data_result("Agent not found.")
    if not UserCanvasService.query(user_id=tenant_id, id=agent_id):
        return get_error_data_result("You cannot access the agent.")
    if not isinstance(cvs.dsl, str):
        cvs.dsl = json.dumps(cvs.dsl, ensure_ascii=False)

    canvas = Canvas(cvs.dsl, tenant_id)
    canvas.reset()
    query = canvas.get_preset_param()
    if query:
        for ele in query:
            if not ele["optional"]:
                if ele["type"] == "file":
                    if files is None or not files.get(ele["key"]):
                        return get_error_data_result(f"`{ele['key']}` with type `{ele['type']}` is required")
                    upload_file = files.get(ele["key"])
                    file_content = FileService.parse_docs([upload_file], user_id)
                    file_name = upload_file.filename
                    ele["value"] = file_name + "\n" + file_content
                else:
                    if req is None or not req.get(ele["key"]):
                        return get_error_data_result(f"`{ele['key']}` with type `{ele['type']}` is required")
                    ele["value"] = req[ele["key"]]
            else:
                if ele["type"] == "file":
                    if files is not None and files.get(ele["key"]):
                        upload_file = files.get(ele["key"])
                        file_content = FileService.parse_docs([upload_file], user_id)
                        file_name = upload_file.filename
                        ele["value"] = file_name + "\n" + file_content
                    else:
                        if "value" in ele:
                            ele.pop("value")
                else:
                    if req is not None and req.get(ele["key"]):
                        ele["value"] = req[ele["key"]]
                    else:
                        if "value" in ele:
                            ele.pop("value")

    for ans in canvas.run(stream=False):
        pass

    cvs.dsl = json.loads(str(canvas))
    conv = {"id": get_uuid(), "dialog_id": cvs.id, "user_id": user_id, "message": [{"role": "assistant", "content": canvas.get_prologue()}], "source": "agent", "dsl": cvs.dsl}
    API4ConversationService.save(**conv)
    conv["agent_id"] = conv.pop("dialog_id")
    return get_result(data=conv)


@manager.route("/chats/<chat_id>/sessions/<session_id>", methods=["PUT"])  # noqa: F821
@token_required
def update(tenant_id, chat_id, session_id):
    req = request.json
    req["dialog_id"] = chat_id
    conv_id = session_id
    conv = ConversationService.query(id=conv_id, dialog_id=chat_id)
    if not conv:
        return get_error_data_result(message="Session does not exist")
    if not DialogService.query(id=chat_id, tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(message="You do not own the session")
    if "message" in req or "messages" in req:
        return get_error_data_result(message="`message` can not be change")
    if "reference" in req:
        return get_error_data_result(message="`reference` can not be change")
    if "name" in req and not req.get("name"):
        return get_error_data_result(message="`name` can not be empty.")
    if not ConversationService.update_by_id(conv_id, req):
        return get_error_data_result(message="Session updates error")
    return get_result()


@manager.route("/chats/<chat_id>/completions", methods=["POST"])  # noqa: F821
@token_required
def chat_completion(tenant_id, chat_id):
    req = request.json
    if not req:
        req = {"question": ""}
    if not req.get("session_id"):
        req["question"] = ""
    if not DialogService.query(tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value):
        return get_error_data_result(f"You don't own the chat {chat_id}")
    if req.get("session_id"):
        if not ConversationService.query(id=req["session_id"], dialog_id=chat_id):
            return get_error_data_result(f"You don't own the session {req['session_id']}")
    if req.get("stream", True):
        resp = Response(rag_completion(tenant_id, chat_id, **req), mimetype="text/event-stream")
        resp.headers.add_header("Cache-control", "no-cache")
        resp.headers.add_header("Connection", "keep-alive")
        resp.headers.add_header("X-Accel-Buffering", "no")
        resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")

        return resp
    else:
        answer = None
        for ans in rag_completion(tenant_id, chat_id, **req):
            answer = ans
            break
        return get_result(data=answer)


@manager.route("/chats_openai/<chat_id>/chat/completions", methods=["POST"])  # noqa: F821
@validate_request("model", "messages")  # noqa: F821
@token_required
def chat_completion_openai_like(tenant_id, chat_id):
    """
    OpenAI-like chat completion API that simulates the behavior of OpenAI's completions endpoint.

    This function allows users to interact with a model and receive responses based on a series of historical messages.
    If `stream` is set to True (by default), the response will be streamed in chunks, mimicking the OpenAI-style API.
    Set `stream` to False explicitly, the response will be returned in a single complete answer.

    Reference:

    - If `stream` is True, the final answer and reference information will appear in the **last chunk** of the stream.
    - If `stream` is False, the reference will be included in `choices[0].message.reference`.

    Example usage:

    curl -X POST https://ragflow_address.com/api/v1/chats_openai/<chat_id>/chat/completions \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $RAGFLOW_API_KEY" \
        -d '{
            "model": "model",
            "messages": [{"role": "user", "content": "Say this is a test!"}],
            "stream": true
        }'

    Alternatively, you can use Python's `OpenAI` client:

    from openai import OpenAI

    model = "model"
    client = OpenAI(api_key="ragflow-api-key", base_url=f"http://ragflow_address/api/v1/chats_openai/<chat_id>")

    stream = True
    reference = True

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Who are you?"},
            {"role": "assistant", "content": "I am an AI assistant named..."},
            {"role": "user", "content": "Can you tell me how to install neovim"},
        ],
        stream=stream,
        extra_body={"reference": reference}
    )

    if stream:
    for chunk in completion:
        print(chunk)
        if reference and chunk.choices[0].finish_reason == "stop":
            print(f"Reference:\n{chunk.choices[0].delta.reference}")
            print(f"Final content:\n{chunk.choices[0].delta.final_content}")
    else:
        print(completion.choices[0].message.content)
        if reference:
            print(completion.choices[0].message.reference)
    """
    req = request.get_json()

    need_reference = bool(req.get("reference", False))

    messages = req.get("messages", [])
    # To prevent empty [] input
    if len(messages) < 1:
        return get_error_data_result("You have to provide messages.")
    if messages[-1]["role"] != "user":
        return get_error_data_result("The last content of this conversation is not from user.")

    prompt = messages[-1]["content"]
    # Treat context tokens as reasoning tokens
    context_token_used = sum(len(message["content"]) for message in messages)

    dia = DialogService.query(tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value)
    if not dia:
        return get_error_data_result(f"You don't own the chat {chat_id}")
    dia = dia[0]

    # Filter system and non-sense assistant messages
    msg = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append(m)

    # tools = get_tools()
    # toolcall_session = SimpleFunctionCallServer()
    tools = None
    toolcall_session = None

    if req.get("stream", True):
        # The value for the usage field on all chunks except for the last one will be null.
        # The usage field on the last chunk contains token usage statistics for the entire request.
        # The choices field on the last chunk will always be an empty array [].
        def streamed_response_generator(chat_id, dia, msg):
            token_used = 0
            answer_cache = ""
            reasoning_cache = ""
            last_ans = {}
            response = {
                "id": f"chatcmpl-{chat_id}",
                "choices": [
                    {
                        "delta": {
                            "content": "",
                            "role": "assistant",
                            "function_call": None,
                            "tool_calls": None,
                            "reasoning_content": "",
                        },
                        "finish_reason": None,
                        "index": 0,
                        "logprobs": None,
                    }
                ],
                "created": int(time.time()),
                "model": "model",
                "object": "chat.completion.chunk",
                "system_fingerprint": "",
                "usage": None,
            }

            try:
                for ans in chat(dia, msg, True, toolcall_session=toolcall_session, tools=tools, quote=need_reference):
                    last_ans = ans
                    answer = ans["answer"]

                    reasoning_match = re.search(r"<think>(.*?)</think>", answer, flags=re.DOTALL)
                    if reasoning_match:
                        reasoning_part = reasoning_match.group(1)
                        content_part = answer[reasoning_match.end() :]
                    else:
                        reasoning_part = ""
                        content_part = answer

                    reasoning_incremental = ""
                    if reasoning_part:
                        if reasoning_part.startswith(reasoning_cache):
                            reasoning_incremental = reasoning_part.replace(reasoning_cache, "", 1)
                        else:
                            reasoning_incremental = reasoning_part
                        reasoning_cache = reasoning_part

                    content_incremental = ""
                    if content_part:
                        if content_part.startswith(answer_cache):
                            content_incremental = content_part.replace(answer_cache, "", 1)
                    else:
                        content_incremental = content_part
                    answer_cache = content_part

                    token_used += len(reasoning_incremental) + len(content_incremental)

                    if not any([reasoning_incremental, content_incremental]):
                        continue

                    if reasoning_incremental:
                        response["choices"][0]["delta"]["reasoning_content"] = reasoning_incremental
                    else:
                        response["choices"][0]["delta"]["reasoning_content"] = None

                    if content_incremental:
                        response["choices"][0]["delta"]["content"] = content_incremental
                    else:
                        response["choices"][0]["delta"]["content"] = None

                    yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"
            except Exception as e:
                response["choices"][0]["delta"]["content"] = "**ERROR**: " + str(e)
                yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"

            # The last chunk
            response["choices"][0]["delta"]["content"] = None
            response["choices"][0]["delta"]["reasoning_content"] = None
            response["choices"][0]["finish_reason"] = "stop"
            response["usage"] = {"prompt_tokens": len(prompt), "completion_tokens": token_used, "total_tokens": len(prompt) + token_used}
            if need_reference:
                response["choices"][0]["delta"]["reference"] = chunks_format(last_ans.get("reference", []))
                response["choices"][0]["delta"]["final_content"] = last_ans.get("answer", "")
            yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"
            yield "data:[DONE]\n\n"

        resp = Response(streamed_response_generator(chat_id, dia, msg), mimetype="text/event-stream")
        resp.headers.add_header("Cache-control", "no-cache")
        resp.headers.add_header("Connection", "keep-alive")
        resp.headers.add_header("X-Accel-Buffering", "no")
        resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")
        return resp
    else:
        answer = None
        for ans in chat(dia, msg, False, toolcall_session=toolcall_session, tools=tools, quote=need_reference):
            # focus answer content only
            answer = ans
            break
        content = answer["answer"]

        response = {
            "id": f"chatcmpl-{chat_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model", ""),
            "usage": {
                "prompt_tokens": len(prompt),
                "completion_tokens": len(content),
                "total_tokens": len(prompt) + len(content),
                "completion_tokens_details": {
                    "reasoning_tokens": context_token_used,
                    "accepted_prediction_tokens": len(content),
                    "rejected_prediction_tokens": 0,  # 0 for simplicity
                },
            },
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "logprobs": None,
                    "finish_reason": "stop",
                    "index": 0,
                }
            ],
        }
        if need_reference:
            response["choices"][0]["message"]["reference"] = chunks_format(answer.get("reference", []))

        return jsonify(response)


@manager.route("/agents_openai/<agent_id>/chat/completions", methods=["POST"])  # noqa: F821
@validate_request("model", "messages")  # noqa: F821
@token_required
def agents_completion_openai_compatibility(tenant_id, agent_id):
    req = request.json
    tiktokenenc = tiktoken.get_encoding("cl100k_base")
    messages = req.get("messages", [])
    if not messages:
        return get_error_data_result("You must provide at least one message.")
    if not UserCanvasService.query(user_id=tenant_id, id=agent_id):
        return get_error_data_result(f"You don't own the agent {agent_id}")

    filtered_messages = [m for m in messages if m["role"] in ["user", "assistant"]]
    prompt_tokens = sum(len(tiktokenenc.encode(m["content"])) for m in filtered_messages)
    if not filtered_messages:
        return jsonify(
            get_data_openai(
                id=agent_id,
                content="No valid messages found (user or assistant).",
                finish_reason="stop",
                model=req.get("model", ""),
                completion_tokens=len(tiktokenenc.encode("No valid messages found (user or assistant).")),
                prompt_tokens=prompt_tokens,
            )
        )

    # Get the last user message as the question
    question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    if req.get("stream", True):
        return Response(completionOpenAI(tenant_id, agent_id, question, session_id=req.get("id", req.get("metadata", {}).get("id", "")), stream=True), mimetype="text/event-stream")
    else:
        # For non-streaming, just return the response directly
        response = next(completionOpenAI(tenant_id, agent_id, question, session_id=req.get("id", req.get("metadata", {}).get("id", "")), stream=False))
        return jsonify(response)


@manager.route("/agents/<agent_id>/completions", methods=["POST"])  # noqa: F821
@token_required
def agent_completions(tenant_id, agent_id):
    req = request.json
    cvs = UserCanvasService.query(user_id=tenant_id, id=agent_id)
    if not cvs:
        return get_error_data_result(f"You don't own the agent {agent_id}")
    if req.get("session_id"):
        dsl = cvs[0].dsl
        if not isinstance(dsl, str):
            dsl = json.dumps(dsl)

        conv = API4ConversationService.query(id=req["session_id"], dialog_id=agent_id)
        if not conv:
            return get_error_data_result(f"You don't own the session {req['session_id']}")
        # If an update to UserCanvas is detected, update the API4Conversation.dsl
        sync_dsl = req.get("sync_dsl", False)
        if sync_dsl is True and cvs[0].update_time > conv[0].update_time:
            current_dsl = conv[0].dsl
            new_dsl = json.loads(dsl)
            state_fields = ["history", "messages", "path", "reference"]
            states = {field: current_dsl.get(field, []) for field in state_fields}
            current_dsl.update(new_dsl)
            current_dsl.update(states)
            API4ConversationService.update_by_id(req["session_id"], {"dsl": current_dsl})
    else:
        req["question"] = ""
    if req.get("stream", True):
        resp = Response(agent_completion(tenant_id, agent_id, **req), mimetype="text/event-stream")
        resp.headers.add_header("Cache-control", "no-cache")
        resp.headers.add_header("Connection", "keep-alive")
        resp.headers.add_header("X-Accel-Buffering", "no")
        resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")
        return resp
    try:
        for answer in agent_completion(tenant_id, agent_id, **req):
            return get_result(data=answer)
    except Exception as e:
        return get_error_data_result(str(e))


@manager.route("/chats/<chat_id>/sessions", methods=["GET"])  # noqa: F821
@token_required
def list_session(tenant_id, chat_id):
    if not DialogService.query(tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value):
        return get_error_data_result(message=f"You don't own the assistant {chat_id}.")
    id = request.args.get("id")
    name = request.args.get("name")
    page_number = int(request.args.get("page", 1))
    items_per_page = int(request.args.get("page_size", 30))
    orderby = request.args.get("orderby", "create_time")
    user_id = request.args.get("user_id")
    if request.args.get("desc") == "False" or request.args.get("desc") == "false":
        desc = False
    else:
        desc = True
    convs = ConversationService.get_list(chat_id, page_number, items_per_page, orderby, desc, id, name, user_id)
    if not convs:
        return get_result(data=[])
    for conv in convs:
        conv["messages"] = conv.pop("message")
        infos = conv["messages"]
        for info in infos:
            if "prompt" in info:
                info.pop("prompt")
        conv["chat_id"] = conv.pop("dialog_id")
        if conv["reference"]:
            messages = conv["messages"]
            message_num = 0
            while message_num < len(messages) and message_num < len(conv["reference"]):
                if message_num != 0 and messages[message_num]["role"] != "user":
                    if message_num >= len(conv["reference"]):
                        break
                    chunk_list = []
                    if "chunks" in conv["reference"][message_num]:
                        chunks = conv["reference"][message_num]["chunks"]
                        for chunk in chunks:
                            new_chunk = {
                                "id": chunk.get("chunk_id", chunk.get("id")),
                                "content": chunk.get("content_with_weight", chunk.get("content")),
                                "document_id": chunk.get("doc_id", chunk.get("document_id")),
                                "document_name": chunk.get("docnm_kwd", chunk.get("document_name")),
                                "dataset_id": chunk.get("kb_id", chunk.get("dataset_id")),
                                "image_id": chunk.get("image_id", chunk.get("img_id")),
                                "positions": chunk.get("positions", chunk.get("position_int")),
                            }

                            chunk_list.append(new_chunk)
                    messages[message_num]["reference"] = chunk_list
                message_num += 1
        del conv["reference"]
    return get_result(data=convs)


@manager.route("/agents/<agent_id>/sessions", methods=["GET"])  # noqa: F821
@token_required
def list_agent_session(tenant_id, agent_id):
    if not UserCanvasService.query(user_id=tenant_id, id=agent_id):
        return get_error_data_result(message=f"You don't own the agent {agent_id}.")
    id = request.args.get("id")
    user_id = request.args.get("user_id")
    page_number = int(request.args.get("page", 1))
    items_per_page = int(request.args.get("page_size", 30))
    orderby = request.args.get("orderby", "update_time")
    if request.args.get("desc") == "False" or request.args.get("desc") == "false":
        desc = False
    else:
        desc = True
    # dsl defaults to True in all cases except for False and false
    include_dsl = request.args.get("dsl") != "False" and request.args.get("dsl") != "false"
    total, convs = API4ConversationService.get_list(agent_id, tenant_id, page_number, items_per_page, orderby, desc, id, user_id, include_dsl)
    if not convs:
        return get_result(data=[])
    for conv in convs:
        conv["messages"] = conv.pop("message")
        infos = conv["messages"]
        for info in infos:
            if "prompt" in info:
                info.pop("prompt")
        conv["agent_id"] = conv.pop("dialog_id")
        if conv["reference"]:
            messages = conv["messages"]
            message_num = 0
            chunk_num = 0
            while message_num < len(messages):
                if message_num != 0 and messages[message_num]["role"] != "user":
                    chunk_list = []
                    if "chunks" in conv["reference"][chunk_num]:
                        chunks = conv["reference"][chunk_num]["chunks"]
                        for chunk in chunks:
                            new_chunk = {
                                "id": chunk.get("chunk_id", chunk.get("id")),
                                "content": chunk.get("content_with_weight", chunk.get("content")),
                                "document_id": chunk.get("doc_id", chunk.get("document_id")),
                                "document_name": chunk.get("docnm_kwd", chunk.get("document_name")),
                                "dataset_id": chunk.get("kb_id", chunk.get("dataset_id")),
                                "image_id": chunk.get("image_id", chunk.get("img_id")),
                                "positions": chunk.get("positions", chunk.get("position_int")),
                            }
                            chunk_list.append(new_chunk)
                    chunk_num += 1
                    messages[message_num]["reference"] = chunk_list
                message_num += 1
        del conv["reference"]
    return get_result(data=convs)


@manager.route("/chats/<chat_id>/sessions", methods=["DELETE"])  # noqa: F821
@token_required
def delete(tenant_id, chat_id):
    if not DialogService.query(id=chat_id, tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(message="You don't own the chat")

    errors = []
    success_count = 0
    req = request.json
    convs = ConversationService.query(dialog_id=chat_id)
    if not req:
        ids = None
    else:
        ids = req.get("ids")

    if not ids:
        conv_list = []
        for conv in convs:
            conv_list.append(conv.id)
    else:
        conv_list = ids

    unique_conv_ids, duplicate_messages = check_duplicate_ids(conv_list, "session")
    conv_list = unique_conv_ids

    for id in conv_list:
        conv = ConversationService.query(id=id, dialog_id=chat_id)
        if not conv:
            errors.append(f"The chat doesn't own the session {id}")
            continue
        ConversationService.delete_by_id(id)
        success_count += 1

    if errors:
        if success_count > 0:
            return get_result(data={"success_count": success_count, "errors": errors}, message=f"Partially deleted {success_count} sessions with {len(errors)} errors")
        else:
            return get_error_data_result(message="; ".join(errors))

    if duplicate_messages:
        if success_count > 0:
            return get_result(message=f"Partially deleted {success_count} sessions with {len(duplicate_messages)} errors", data={"success_count": success_count, "errors": duplicate_messages})
        else:
            return get_error_data_result(message=";".join(duplicate_messages))

    return get_result()


@manager.route("/agents/<agent_id>/sessions", methods=["DELETE"])  # noqa: F821
@token_required
def delete_agent_session(tenant_id, agent_id):
    errors = []
    success_count = 0
    req = request.json
    cvs = UserCanvasService.query(user_id=tenant_id, id=agent_id)
    if not cvs:
        return get_error_data_result(f"You don't own the agent {agent_id}")

    convs = API4ConversationService.query(dialog_id=agent_id)
    if not convs:
        return get_error_data_result(f"Agent {agent_id} has no sessions")

    if not req:
        ids = None
    else:
        ids = req.get("ids")

    if not ids:
        conv_list = []
        for conv in convs:
            conv_list.append(conv.id)
    else:
        conv_list = ids

    unique_conv_ids, duplicate_messages = check_duplicate_ids(conv_list, "session")
    conv_list = unique_conv_ids

    for session_id in conv_list:
        conv = API4ConversationService.query(id=session_id, dialog_id=agent_id)
        if not conv:
            errors.append(f"The agent doesn't own the session {session_id}")
            continue
        API4ConversationService.delete_by_id(session_id)
        success_count += 1

    if errors:
        if success_count > 0:
            return get_result(data={"success_count": success_count, "errors": errors}, message=f"Partially deleted {success_count} sessions with {len(errors)} errors")
        else:
            return get_error_data_result(message="; ".join(errors))

    if duplicate_messages:
        if success_count > 0:
            return get_result(message=f"Partially deleted {success_count} sessions with {len(duplicate_messages)} errors", data={"success_count": success_count, "errors": duplicate_messages})
        else:
            return get_error_data_result(message=";".join(duplicate_messages))

    return get_result()


@manager.route("/sessions/ask", methods=["POST"])  # noqa: F821
@token_required
def ask_about(tenant_id):
    req = request.json
    if not req.get("question"):
        return get_error_data_result("`question` is required.")
    if not req.get("dataset_ids"):
        return get_error_data_result("`dataset_ids` is required.")
    if not isinstance(req.get("dataset_ids"), list):
        return get_error_data_result("`dataset_ids` should be a list.")
    req["kb_ids"] = req.pop("dataset_ids")
    for kb_id in req["kb_ids"]:
        if not KnowledgebaseService.accessible(kb_id, tenant_id):
            return get_error_data_result(f"You don't own the dataset {kb_id}.")
        kbs = KnowledgebaseService.query(id=kb_id)
        kb = kbs[0]
        if kb.chunk_num == 0:
            return get_error_data_result(f"The dataset {kb_id} doesn't own parsed file")
    uid = tenant_id

    def stream():
        nonlocal req, uid
        try:
            for ans in ask(req["question"], req["kb_ids"], uid):
                yield "data:" + json.dumps({"code": 0, "message": "", "data": ans}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    resp = Response(stream(), mimetype="text/event-stream")
    resp.headers.add_header("Cache-control", "no-cache")
    resp.headers.add_header("Connection", "keep-alive")
    resp.headers.add_header("X-Accel-Buffering", "no")
    resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")
    return resp


@manager.route("/sessions/related_questions", methods=["POST"])  # noqa: F821
@token_required
def related_questions(tenant_id):
    req = request.json
    if not req.get("question"):
        return get_error_data_result("`question` is required.")
    question = req["question"]
    industry = req.get("industry", "")
    chat_mdl = LLMBundle(tenant_id, LLMType.CHAT)
    prompt = """
Objective: To generate search terms related to the user's search keywords, helping users find more valuable information.
Instructions:
 - Based on the keywords provided by the user, generate 5-10 related search terms.
 - Each search term should be directly or indirectly related to the keyword, guiding the user to find more valuable information.
 - Use common, general terms as much as possible, avoiding obscure words or technical jargon.
 - Keep the term length between 2-4 words, concise and clear.
 - DO NOT translate, use the language of the original keywords.
"""
    if industry:
        prompt += f" - Ensure all search terms are relevant to the industry: {industry}.\n"
    prompt += """
### Example:
Keywords: Chinese football
Related search terms:
1. Current status of Chinese football
2. Reform of Chinese football
3. Youth training of Chinese football
4. Chinese football in the Asian Cup
5. Chinese football in the World Cup

Reason:
 - When searching, users often only use one or two keywords, making it difficult to fully express their information needs.
 - Generating related search terms can help users dig deeper into relevant information and improve search efficiency.
 - At the same time, related terms can also help search engines better understand user needs and return more accurate search results.

"""
    ans = chat_mdl.chat(
        prompt,
        [
            {
                "role": "user",
                "content": f"""
Keywords: {question}
Related search terms:
    """,
            }
        ],
        {"temperature": 0.9},
    )
    return get_result(data=[re.sub(r"^[0-9]\. ", "", a) for a in ans.split("\n") if re.match(r"^[0-9]\. ", a)])


@manager.route("/chatbots/<dialog_id>/completions", methods=["POST"])  # noqa: F821
def chatbot_completions(dialog_id):
    req = request.json

    token = request.headers.get("Authorization").split()
    if len(token) != 2:
        return get_error_data_result(message='Authorization is not valid!"')
    token = token[1]
    objs = APIToken.query(beta=token)
    if not objs:
        return get_error_data_result(message='Authentication error: API key is invalid!"')

    if "quote" not in req:
        req["quote"] = False

    if req.get("stream", True):
        resp = Response(iframe_completion(dialog_id, **req), mimetype="text/event-stream")
        resp.headers.add_header("Cache-control", "no-cache")
        resp.headers.add_header("Connection", "keep-alive")
        resp.headers.add_header("X-Accel-Buffering", "no")
        resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")
        return resp

    for answer in iframe_completion(dialog_id, **req):
        return get_result(data=answer)


@manager.route("/agentbots/<agent_id>/completions", methods=["POST"])  # noqa: F821
def agent_bot_completions(agent_id):
    req = request.json

    token = request.headers.get("Authorization").split()
    if len(token) != 2:
        return get_error_data_result(message='Authorization is not valid!"')
    token = token[1]
    objs = APIToken.query(beta=token)
    if not objs:
        return get_error_data_result(message='Authentication error: API key is invalid!"')

    if req.get("stream", True):
        resp = Response(agent_completion(objs[0].tenant_id, agent_id, **req), mimetype="text/event-stream")
        resp.headers.add_header("Cache-control", "no-cache")
        resp.headers.add_header("Connection", "keep-alive")
        resp.headers.add_header("X-Accel-Buffering", "no")
        resp.headers.add_header("Content-Type", "text/event-stream; charset=utf-8")
        return resp

    for answer in agent_completion(objs[0].tenant_id, agent_id, **req):
        return get_result(data=answer)


@manager.route("/agentbots/<agent_id>/inputs", methods=["GET"])  # noqa: F821
def begin_inputs(agent_id):
    token = request.headers.get("Authorization").split()
    if len(token) != 2:
        return get_error_data_result(message='Authorization is not valid!"')
    token = token[1]
    objs = APIToken.query(beta=token)
    if not objs:
        return get_error_data_result(message='Authentication error: API key is invalid!"')

    e, cvs = UserCanvasService.get_by_id(agent_id)
    if not e:
        return get_error_data_result(f"Can't find agent by ID: {agent_id}")

    canvas = Canvas(json.dumps(cvs.dsl), objs[0].tenant_id)
    return get_result(data={
        "title": cvs.title,
        "avatar": cvs.avatar,
        "inputs": canvas.get_component_input_form("begin")
    })



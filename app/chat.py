from .foundry_client import chat as agent_chat


def answer(
    message: str,
    thread_id: str | None = None,
    agent_id: str | None = None,
    model: str | None = None,
) -> dict:
    # Version A: pass the user message straight through to the chosen agent.
    # The agent's file_search tool retrieves from the shared vector store and
    # the model's response is returned verbatim. Default Content Safety
    # filters on the model deployment are the only safety layer; version B
    # will wrap this call with a custom firewall.
    return agent_chat(message, thread_id=thread_id, agent_id=agent_id, model=model)

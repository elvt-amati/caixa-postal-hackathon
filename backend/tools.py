"""Agent tools — actions the bots can take.

Public catalog lives in TOOLS_BY_NAME so agents.yaml can reference each by name.
"""
import os

import boto3
from strands import tool

from store import put_item, list_by_category
from store import update_item as _store_update
from store import delete_item as _store_delete
from store import undo_delete as _store_undo
from pitch_deck import generate_pitch_deck  # noqa: F401 — registered below

REGION = os.environ.get("AWS_REGION", "us-east-1")
SES_FROM = os.environ.get("SES_FROM", "")

_ses = boto3.client("ses", region_name=REGION)


# -------- Concierge action tools (write-side) -----------------------------


@tool
def create_task(title: str, notes: str = "", due_date: str = "") -> dict:
    """Salva uma tarefa na lista de afazeres."""
    item = put_item("task", title=title, notes=notes, due_date=due_date, status="open")
    return {"ok": True, "id": item["sk"], "message": f"Tarefa '{title}' criada"}


@tool
def create_payment(description: str, amount: float, due_date: str = "", payee: str = "") -> dict:
    """Registra um pagamento/boleto a pagar."""
    item = put_item("payment", description=description, amount=float(amount), due_date=due_date, payee=payee)
    return {"ok": True, "id": item["sk"], "message": f"Pagamento '{description}' de R$ {amount:.2f} registrado"}


@tool
def create_reminder(title: str, at: str, notes: str = "") -> dict:
    """Cria lembrete para data/hora futuros (at: ISO-8601)."""
    item = put_item("reminder", title=title, at=at, notes=notes)
    return {"ok": True, "id": item["sk"], "message": f"Lembrete '{title}' marcado para {at}"}


@tool
def save_contact(name: str, phone: str = "", email: str = "", notes: str = "") -> dict:
    """Salva um contato."""
    item = put_item("contact", name=name, phone=phone, email=email, notes=notes)
    return {"ok": True, "id": item["sk"], "message": f"Contato '{name}' salvo"}


@tool
def save_note(title: str, body: str) -> dict:
    """Salva uma anotação/ideia."""
    item = put_item("note", title=title, body=body)
    return {"ok": True, "id": item["sk"], "message": f"Nota '{title}' salva"}


@tool
def send_notification_email(to: str, subject: str, body: str) -> dict:
    """Envia email (use só se o usuário pedir explicitamente)."""
    if not SES_FROM:
        return {"ok": False, "message": "SES_FROM não configurado"}
    try:
        _ses.send_email(
            Source=SES_FROM,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
        put_item("email", to=to, subject=subject, body=body[:500], status="sent")
        return {"ok": True, "message": f"Email enviado para {to}"}
    except Exception as e:
        put_item("email", to=to, subject=subject, body=body[:500], status=f"error: {e}")
        return {"ok": False, "message": f"Falha no envio: {e}"}


# -------- Specialist read tools ------------------------------------------


CANONICAL_FIELDS = {
    "task":     ["title", "notes", "due_date", "status"],
    "payment":  ["description", "amount", "due_date", "payee", "status"],
    "reminder": ["title", "at", "notes"],
    "contact":  ["name", "phone", "email", "notes"],
    "note":     ["title", "body"],
    "email":    ["to", "subject", "body", "status"],
}


@tool
def update_item(category: str, id: str, fields: dict) -> dict:
    """Atualiza campos de um item existente. Use em vez de criar um novo.

    Campos canônicos por categoria (use APENAS estes):
    - task: title, notes, due_date, status
    - payment: description, amount, due_date, payee, status
    - reminder: title, at, notes
    - contact: name, phone, email, notes
    - note: title, body

    Args:
        category: categoria do item (task, payment, reminder, contact, note).
        id: o sk retornado pelo list_*.
        fields: dict com os campos a atualizar. Só passe os que QUER mudar.

    Exemplo: para adiar um pagamento, fields={"due_date": "2026-05-10"}.
    """
    if category not in CANONICAL_FIELDS:
        return {"ok": False, "message": f"categoria desconhecida: {category}"}
    valid = {k: v for k, v in (fields or {}).items() if k in CANONICAL_FIELDS[category]}
    if not valid:
        return {"ok": False, "message": "nenhum campo válido fornecido"}
    updated = _store_update(category, id, valid)
    if not updated:
        return {"ok": False, "message": f"item {id} não encontrado"}
    return {"ok": True, "id": id, "updated": valid, "message": f"Item atualizado: {', '.join(valid.keys())}"}


@tool
def delete_item(category: str, id: str) -> dict:
    """Apaga (soft-delete) um item. O item some das listas mas pode ser recuperado.

    Args:
        category: categoria (task, payment, reminder, contact, note).
        id: o sk do item a apagar.
    """
    if category not in CANONICAL_FIELDS:
        return {"ok": False, "message": f"categoria desconhecida: {category}"}
    res = _store_delete(category, id, hard=False)
    return {"ok": True, "id": id, "message": f"{category} apagado (pode desfazer)"}


@tool
def undo_delete(category: str, id: str) -> dict:
    """Desfaz um soft-delete, restaurando o item.

    Args:
        category: categoria do item.
        id: o sk do item apagado.
    """
    restored = _store_undo(category, id)
    if not restored:
        return {"ok": False, "message": f"item {id} não encontrado"}
    return {"ok": True, "id": id, "message": f"{category} restaurado"}


@tool
def list_tasks(limit: int = 20) -> list:
    """Lista tarefas abertas do usuário."""
    return list_by_category("task", limit=limit)


@tool
def list_payments(limit: int = 20) -> list:
    """Lista pagamentos/boletos registrados."""
    return list_by_category("payment", limit=limit)


@tool
def list_reminders(limit: int = 20) -> list:
    """Lista lembretes agendados."""
    return list_by_category("reminder", limit=limit)


@tool
def render_chart(
    chart_type: str,
    title: str,
    labels: list[str],
    values: list[float],
    unit: str = "",
) -> dict:
    """Renderiza um gráfico interativo no chat (generative UI via AG-UI).

    Use quando o usuário pedir "gráfico", "visualiza", "plota", "mostra em chart"
    ou quando a resposta ficaria mais clara com uma visualização.

    Args:
        chart_type: tipo de gráfico — 'bar', 'line', 'pie', ou 'doughnut'.
        title: título curto do gráfico.
        labels: rótulos do eixo X (ou fatias na pie).
        values: valores numéricos correspondentes.
        unit: prefixo de unidade (ex: 'R$ ', '%'). Opcional.

    Retorna um spec que o frontend intercepta e renderiza com Chart.js —
    é o usuário do agente que vê o gráfico dentro do próprio chat.
    """
    return {
        "ok": True,
        "chart_type": chart_type,
        "title": title,
        "labels": labels,
        "values": [float(v) for v in values],
        "unit": unit,
    }


@tool
def publish_card(
    published_by: str,
    key: str,
    template: str,
    title: str,
    spec: dict,
) -> dict:
    """Publica um card persistente na home do usuário (custom AG-UI generative UI).

    Use quando o usuário pedir "coloca no home", "deixa fixo", "cria um card",
    ou quando você acha que a informação é valiosa de ter sempre visível.

    Args:
        published_by: nome do agente que está publicando (ex: 'financeiro', 'pesquisa').
        key: identificador único do card dentro desse agente (ex: 'top_payees', 'week_progress').
             Se já existir, atualiza.
        template: um de 'list', 'chart', 'metric'. (kanban/calendar só pra cards canônicos por enquanto.)
        title: título curto exibido no card.
        spec: dados do card. Para 'chart': {chart_type, labels, values, unit}. Para 'metric': {value, unit, sub}. Para 'list': {items:[{title, sub}]}.

    O frontend intercepta esse tool result e persiste em localStorage — o card sobrevive refresh.
    """
    return {
        "ok": True,
        "published_by": published_by,
        "agent": published_by,  # alias for frontend
        "key": key,
        "card": {
            "template": template,
            "title": title,
            "spec": spec,
            "updated_at": int(__import__("time").time()),
        },
    }


@tool
def search_web(query: str) -> list:
    """(DEMO) Busca simulada. Em produção, plugue um MCP server de busca real (Exa, Brave, Google)."""
    samples = [
        {"title": "Resultado demo sobre " + query, "url": "https://example.com/1",
         "snippet": "Este é um resultado sintético para fins de demonstração."},
        {"title": "Guia introdutório: " + query, "url": "https://example.com/2",
         "snippet": "Agente de pesquisa pode plugar MCP server real aqui."},
    ]
    return samples


TOOLS_BY_NAME = {
    "create_task": create_task,
    "create_payment": create_payment,
    "create_reminder": create_reminder,
    "save_contact": save_contact,
    "save_note": save_note,
    "send_notification_email": send_notification_email,
    "list_tasks": list_tasks,
    "list_payments": list_payments,
    "list_reminders": list_reminders,
    "search_web": search_web,
    "render_chart": render_chart,
    "publish_card": publish_card,
    "generate_pitch_deck": generate_pitch_deck,
    "update_item": update_item,
    "delete_item": delete_item,
    "undo_delete": undo_delete,
}

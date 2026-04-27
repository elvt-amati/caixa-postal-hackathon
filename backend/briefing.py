"""On-demand morning briefing endpoint — simulates the 'scheduled assistant'.

When the user opens the chat, the frontend calls /api/briefing. The backend
uses the Agenda specialist to summarize what's pending. This makes it feel
like the assistant proactively pushed the daily digest.
"""
from datetime import datetime


def briefing_prompt() -> str:
    today = datetime.now().strftime("%A, %d/%m/%Y")
    return (
        f"Hoje é {today}. Gera o briefing matinal do usuário. "
        "Olhe lembretes, pagamentos vencendo em até 7 dias, e tarefas abertas. "
        "Use o formato: 'Bom dia! ☀️ Resumo de hoje:' + até 5 bullets. "
        "Se não houver nada registrado, diga isso com uma frase amigável e sugira começar adicionando algo."
    )

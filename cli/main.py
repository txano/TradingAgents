from typing import Optional
import datetime
import threading
import typer
from pathlib import Path
from functools import wraps
from rich.console import Console
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
load_dotenv(".env.enterprise", override=False)
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live
from rich.columns import Columns
from rich.markdown import Markdown
from rich.layout import Layout
from rich.text import Text
from rich.table import Table
from collections import deque
import time
from rich.tree import Tree
from rich import box
from rich.align import Align
from rich.rule import Rule

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.earnings import EarningsLayer
from tradingagents.reflection import ReflectionLayer
from tradingagents.reflection.layer import parse_reflection_score
from tradingagents.allocation import AllocationLayer
from tradingagents.allocation.layer import parse_allocation
from cli.models import AnalystType
from cli.utils import *
from cli.announcements import fetch_announcements, display_announcements
from cli.stats_handler import StatsCallbackHandler

console = Console()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def main_menu(ctx: typer.Context) -> None:
    """Launch the interactive menu when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return  # A subcommand was provided — let Typer handle it normally

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents[/bold green]  [dim]Multi-Agent LLM Trading Framework[/dim]",
        border_style="green", padding=(0, 2),
    ))

    _SEP = questionary.Separator
    choices = [
        _SEP("── Research & Analysis ──────────────────"),
        questionary.Choice("  Calendar   earnings calendar → auto-screen",   value="calendar"),
        questionary.Choice("  Analyze    single ticker deep-dive",           value="analyze"),
        questionary.Choice("  Screen     batch earnings screen + allocate",  value="screen"),
        questionary.Choice("  Allocate   re-run council on existing screen", value="allocate"),
        _SEP("── Trades & Reflections ─────────────────"),
        questionary.Choice("  Import     IBKR Flex trade import",            value="import"),
        questionary.Choice("  Trades     view trade history",                value="trades"),
        questionary.Choice("  Reflect    post-mortem on a trade",            value="reflect"),
        questionary.Choice("  Calibrate  predictions vs actual outcomes",    value="calibrate"),
        _SEP("── System Improvement ───────────────────"),
        questionary.Choice("  Improve     LLM analysis of reflections",           value="improve"),
        questionary.Choice("  Correlation score-to-outcome correlation analysis", value="correlation"),
        questionary.Choice("  Stats       win rate & accuracy summary",           value="stats"),
        questionary.Choice("  Weights     view / adjust allocation weights",      value="weights"),
        _SEP("── Other ────────────────────────────────"),
        questionary.Choice("  Dashboard  launch local web dashboard",        value="dashboard"),
        questionary.Choice("  Build Web  build static reports website",      value="build-web"),
        _SEP(""),
        questionary.Choice("  Exit",                                         value="exit"),
    ]

    selection = questionary.select(
        "What would you like to do?",
        choices=choices,
        use_shortcuts=False,
        style=questionary.Style([
            ("highlighted", "fg:cyan bold"),
            ("selected",    "fg:cyan"),
            ("separator",   "fg:#555555 bold"),
        ]),
    ).ask()

    if not selection or selection == "exit":
        return

    dispatch = {
        "calendar":  lambda: earnings_calendar(date=None, source="nasdaq", min_cap=None, all_caps=False, budget=100_000),
        "analyze":   lambda: analyze(),
        "screen":    lambda: screen(budget=100_000),
        "allocate":  lambda: allocate(budget=100_000, dir=None),
        "import":    lambda: import_ibkr(file=None, all_trades=False),
        "trades":    lambda: trades(),
        "reflect":   lambda: reflect(),
        "calibrate": lambda: calibrate(),
        "improve":      lambda: improve(),
        "correlation":  lambda: correlation(),
        "stats":        lambda: stats(),
        "weights":   lambda: allocation_weights(beat=None, guidance=None, setup=None, reset=False),
        "dashboard": lambda: dashboard(port=8765, no_browser=False),
        "build-web": lambda: build_web(),
    }
    dispatch[selection]()


# Create a deque to store recent messages with a maximum length
class MessageBuffer:
    # Fixed teams that always run (not user-selectable)
    FIXED_AGENTS = {
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Analyst name mapping
    ANALYST_MAPPING = {
        "market": "Market Analyst",
        "social": "Social Analyst",
        "news": "News Analyst",
        "fundamentals": "Fundamentals Analyst",
    }

    # Report section mapping: section -> (analyst_key for filtering, finalizing_agent)
    # analyst_key: which analyst selection controls this section (None = always included)
    # finalizing_agent: which agent must be "completed" for this report to count as done
    REPORT_SECTIONS = {
        "market_report": ("market", "Market Analyst"),
        "sentiment_report": ("social", "Social Analyst"),
        "news_report": ("news", "News Analyst"),
        "fundamentals_report": ("fundamentals", "Fundamentals Analyst"),
        "investment_plan": (None, "Research Manager"),
        "trader_investment_plan": (None, "Trader"),
        "final_trade_decision": (None, "Portfolio Manager"),
    }

    def __init__(self, max_length=100):
        self.messages = deque(maxlen=max_length)
        self.tool_calls = deque(maxlen=max_length)
        self.current_report = None
        self.final_report = None  # Store the complete final report
        self.agent_status = {}
        self.current_agent = None
        self.report_sections = {}
        self.selected_analysts = []
        self._processed_message_ids = set()

    def init_for_analysis(self, selected_analysts):
        """Initialize agent status and report sections based on selected analysts.

        Args:
            selected_analysts: List of analyst type strings (e.g., ["market", "news"])
        """
        self.selected_analysts = [a.lower() for a in selected_analysts]

        # Build agent_status dynamically
        self.agent_status = {}

        # Add selected analysts
        for analyst_key in self.selected_analysts:
            if analyst_key in self.ANALYST_MAPPING:
                self.agent_status[self.ANALYST_MAPPING[analyst_key]] = "pending"

        # Add fixed teams
        for team_agents in self.FIXED_AGENTS.values():
            for agent in team_agents:
                self.agent_status[agent] = "pending"

        # Build report_sections dynamically
        self.report_sections = {}
        for section, (analyst_key, _) in self.REPORT_SECTIONS.items():
            if analyst_key is None or analyst_key in self.selected_analysts:
                self.report_sections[section] = None

        # Reset other state
        self.current_report = None
        self.final_report = None
        self.current_agent = None
        self.messages.clear()
        self.tool_calls.clear()
        self._processed_message_ids.clear()

    def get_completed_reports_count(self):
        """Count reports that are finalized (their finalizing agent is completed).

        A report is considered complete when:
        1. The report section has content (not None), AND
        2. The agent responsible for finalizing that report has status "completed"

        This prevents interim updates (like debate rounds) from counting as completed.
        """
        count = 0
        for section in self.report_sections:
            if section not in self.REPORT_SECTIONS:
                continue
            _, finalizing_agent = self.REPORT_SECTIONS[section]
            # Report is complete if it has content AND its finalizing agent is done
            has_content = self.report_sections.get(section) is not None
            agent_done = self.agent_status.get(finalizing_agent) == "completed"
            if has_content and agent_done:
                count += 1
        return count

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.messages.append((timestamp, message_type, content))

    def add_tool_call(self, tool_name, args):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.tool_calls.append((timestamp, tool_name, args))

    def update_agent_status(self, agent, status):
        if agent in self.agent_status:
            self.agent_status[agent] = status
            self.current_agent = agent

    def update_report_section(self, section_name, content):
        if section_name in self.report_sections:
            self.report_sections[section_name] = content
            self._update_current_report()

    def _update_current_report(self):
        # For the panel display, only show the most recently updated section
        latest_section = None
        latest_content = None

        # Find the most recently updated section
        for section, content in self.report_sections.items():
            if content is not None:
                latest_section = section
                latest_content = content
               
        if latest_section and latest_content:
            # Format the current section for display
            section_titles = {
                "market_report": "Market Analysis",
                "sentiment_report": "Social Sentiment",
                "news_report": "News Analysis",
                "fundamentals_report": "Fundamentals Analysis",
                "investment_plan": "Research Team Decision",
                "trader_investment_plan": "Trading Team Plan",
                "final_trade_decision": "Portfolio Management Decision",
            }
            self.current_report = (
                f"### {section_titles[latest_section]}\n{latest_content}"
            )

        # Update the final complete report
        self._update_final_report()

    def _update_final_report(self):
        report_parts = []

        # Analyst Team Reports - use .get() to handle missing sections
        analyst_sections = ["market_report", "sentiment_report", "news_report", "fundamentals_report"]
        if any(self.report_sections.get(section) for section in analyst_sections):
            report_parts.append("## Analyst Team Reports")
            if self.report_sections.get("market_report"):
                report_parts.append(
                    f"### Market Analysis\n{self.report_sections['market_report']}"
                )
            if self.report_sections.get("sentiment_report"):
                report_parts.append(
                    f"### Social Sentiment\n{self.report_sections['sentiment_report']}"
                )
            if self.report_sections.get("news_report"):
                report_parts.append(
                    f"### News Analysis\n{self.report_sections['news_report']}"
                )
            if self.report_sections.get("fundamentals_report"):
                report_parts.append(
                    f"### Fundamentals Analysis\n{self.report_sections['fundamentals_report']}"
                )

        # Research Team Reports
        if self.report_sections.get("investment_plan"):
            report_parts.append("## Research Team Decision")
            report_parts.append(f"{self.report_sections['investment_plan']}")

        # Trading Team Reports
        if self.report_sections.get("trader_investment_plan"):
            report_parts.append("## Trading Team Plan")
            report_parts.append(f"{self.report_sections['trader_investment_plan']}")

        # Portfolio Management Decision
        if self.report_sections.get("final_trade_decision"):
            report_parts.append("## Portfolio Management Decision")
            report_parts.append(f"{self.report_sections['final_trade_decision']}")

        self.final_report = "\n\n".join(report_parts) if report_parts else None


message_buffer = MessageBuffer()


def create_layout():
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_column(
        Layout(name="upper", ratio=3), Layout(name="analysis", ratio=5)
    )
    layout["upper"].split_row(
        Layout(name="progress", ratio=2), Layout(name="messages", ratio=3)
    )
    return layout


def format_tokens(n):
    """Format token count for display."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def update_display(layout, spinner_text=None, stats_handler=None, start_time=None):
    # Header with welcome message
    layout["header"].update(
        Panel(
            "[bold green]Welcome to TradingAgents CLI[/bold green]\n"
            "[dim]© [Tauric Research](https://github.com/TauricResearch)[/dim]",
            title="Welcome to TradingAgents",
            border_style="green",
            padding=(1, 2),
            expand=True,
        )
    )

    # Progress panel showing agent status
    progress_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        box=box.SIMPLE_HEAD,  # Use simple header with horizontal lines
        title=None,  # Remove the redundant Progress title
        padding=(0, 2),  # Add horizontal padding
        expand=True,  # Make table expand to fill available space
    )
    progress_table.add_column("Team", style="cyan", justify="center", width=20)
    progress_table.add_column("Agent", style="green", justify="center", width=20)
    progress_table.add_column("Status", style="yellow", justify="center", width=20)

    # Group agents by team - filter to only include agents in agent_status
    all_teams = {
        "Analyst Team": [
            "Market Analyst",
            "Social Analyst",
            "News Analyst",
            "Fundamentals Analyst",
        ],
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # Filter teams to only include agents that are in agent_status
    teams = {}
    for team, agents in all_teams.items():
        active_agents = [a for a in agents if a in message_buffer.agent_status]
        if active_agents:
            teams[team] = active_agents

    for team, agents in teams.items():
        # Add first agent with team name
        first_agent = agents[0]
        status = message_buffer.agent_status.get(first_agent, "pending")
        if status == "in_progress":
            spinner = Spinner(
                "dots", text="[blue]in_progress[/blue]", style="bold cyan"
            )
            status_cell = spinner
        else:
            status_color = {
                "pending": "yellow",
                "completed": "green",
                "error": "red",
            }.get(status, "white")
            status_cell = f"[{status_color}]{status}[/{status_color}]"
        progress_table.add_row(team, first_agent, status_cell)

        # Add remaining agents in team
        for agent in agents[1:]:
            status = message_buffer.agent_status.get(agent, "pending")
            if status == "in_progress":
                spinner = Spinner(
                    "dots", text="[blue]in_progress[/blue]", style="bold cyan"
                )
                status_cell = spinner
            else:
                status_color = {
                    "pending": "yellow",
                    "completed": "green",
                    "error": "red",
                }.get(status, "white")
                status_cell = f"[{status_color}]{status}[/{status_color}]"
            progress_table.add_row("", agent, status_cell)

        # Add horizontal line after each team
        progress_table.add_row("─" * 20, "─" * 20, "─" * 20, style="dim")

    layout["progress"].update(
        Panel(progress_table, title="Progress", border_style="cyan", padding=(1, 2))
    )

    # Messages panel showing recent messages and tool calls
    messages_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        expand=True,  # Make table expand to fill available space
        box=box.MINIMAL,  # Use minimal box style for a lighter look
        show_lines=True,  # Keep horizontal lines
        padding=(0, 1),  # Add some padding between columns
    )
    messages_table.add_column("Time", style="cyan", width=8, justify="center")
    messages_table.add_column("Type", style="green", width=10, justify="center")
    messages_table.add_column(
        "Content", style="white", no_wrap=False, ratio=1
    )  # Make content column expand

    # Combine tool calls and messages
    all_messages = []

    # Add tool calls
    for timestamp, tool_name, args in message_buffer.tool_calls:
        formatted_args = format_tool_args(args)
        all_messages.append((timestamp, "Tool", f"{tool_name}: {formatted_args}"))

    # Add regular messages
    for timestamp, msg_type, content in message_buffer.messages:
        content_str = str(content) if content else ""
        if len(content_str) > 200:
            content_str = content_str[:197] + "..."
        all_messages.append((timestamp, msg_type, content_str))

    # Sort by timestamp descending (newest first)
    all_messages.sort(key=lambda x: x[0], reverse=True)

    # Calculate how many messages we can show based on available space
    max_messages = 12

    # Get the first N messages (newest ones)
    recent_messages = all_messages[:max_messages]

    # Add messages to table (already in newest-first order)
    for timestamp, msg_type, content in recent_messages:
        # Format content with word wrapping
        wrapped_content = Text(content, overflow="fold")
        messages_table.add_row(timestamp, msg_type, wrapped_content)

    layout["messages"].update(
        Panel(
            messages_table,
            title="Messages & Tools",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # Analysis panel showing current report
    if message_buffer.current_report:
        layout["analysis"].update(
            Panel(
                Markdown(message_buffer.current_report),
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )
    else:
        layout["analysis"].update(
            Panel(
                "[italic]Waiting for analysis report...[/italic]",
                title="Current Report",
                border_style="green",
                padding=(1, 2),
            )
        )

    # Footer with statistics
    # Agent progress - derived from agent_status dict
    agents_completed = sum(
        1 for status in message_buffer.agent_status.values() if status == "completed"
    )
    agents_total = len(message_buffer.agent_status)

    # Report progress - based on agent completion (not just content existence)
    reports_completed = message_buffer.get_completed_reports_count()
    reports_total = len(message_buffer.report_sections)

    # Build stats parts
    stats_parts = [f"Agents: {agents_completed}/{agents_total}"]

    # LLM and tool stats from callback handler
    if stats_handler:
        stats = stats_handler.get_stats()
        stats_parts.append(f"LLM: {stats['llm_calls']}")
        stats_parts.append(f"Tools: {stats['tool_calls']}")

        # Token display with graceful fallback
        if stats["tokens_in"] > 0 or stats["tokens_out"] > 0:
            tokens_str = f"Tokens: {format_tokens(stats['tokens_in'])}\u2191 {format_tokens(stats['tokens_out'])}\u2193"
        else:
            tokens_str = "Tokens: --"
        stats_parts.append(tokens_str)

    stats_parts.append(f"Reports: {reports_completed}/{reports_total}")

    # Elapsed time
    if start_time:
        elapsed = time.time() - start_time
        elapsed_str = f"\u23f1 {int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        stats_parts.append(elapsed_str)

    stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats_table.add_column("Stats", justify="center")
    stats_table.add_row(" | ".join(stats_parts))

    layout["footer"].update(Panel(stats_table, border_style="grey50"))


def get_user_selections():
    """Get all user selections before starting the analysis display."""
    # Display ASCII art welcome message
    with open(Path(__file__).parent / "static" / "welcome.txt", "r") as f:
        welcome_ascii = f.read()

    # Create welcome box content
    welcome_content = f"{welcome_ascii}\n"
    welcome_content += "[bold green]TradingAgents: Multi-Agents LLM Financial Trading Framework - CLI[/bold green]\n\n"
    welcome_content += "[bold]Workflow Steps:[/bold]\n"
    welcome_content += "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management\n\n"
    welcome_content += (
        "[dim]Built by [Tauric Research](https://github.com/TauricResearch)[/dim]"
    )

    # Create and center the welcome box
    welcome_box = Panel(
        welcome_content,
        border_style="green",
        padding=(1, 2),
        title="Welcome to TradingAgents",
        subtitle="Multi-Agents LLM Financial Trading Framework",
    )
    console.print(Align.center(welcome_box))
    console.print()
    console.print()  # Add vertical space before announcements

    # Fetch and display announcements (silent on failure)
    announcements = fetch_announcements()
    display_announcements(console, announcements)

    # Create a boxed questionnaire for each step
    def create_question_box(title, prompt, default=None):
        box_content = f"[bold]{title}[/bold]\n"
        box_content += f"[dim]{prompt}[/dim]"
        if default:
            box_content += f"\n[dim]Default: {default}[/dim]"
        return Panel(box_content, border_style="blue", padding=(1, 2))

    # Step 1: Ticker symbol
    console.print(
        create_question_box(
            "Step 1: Ticker Symbol",
            "Enter the exact ticker symbol to analyze, including exchange suffix when needed (examples: SPY, CNC.TO, 7203.T, 0700.HK)",
            "SPY",
        )
    )
    selected_ticker = get_ticker()

    # Step 2: Analysis date
    default_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(
        create_question_box(
            "Step 2: Analysis Date",
            "Enter the analysis date (YYYY-MM-DD)",
            default_date,
        )
    )
    analysis_date = get_analysis_date()

    # Step 3: Output language
    console.print(
        create_question_box(
            "Step 3: Output Language",
            "Select the language for analyst reports and final decision"
        )
    )
    output_language = ask_output_language()

    # Step 4: Select analysts
    console.print(
        create_question_box(
            "Step 4: Analysts Team", "Select your LLM analyst agents for the analysis"
        )
    )
    selected_analysts = select_analysts()
    console.print(
        f"[green]Selected analysts:[/green] {', '.join(analyst.value for analyst in selected_analysts)}"
    )

    # Step 5: Research depth
    console.print(
        create_question_box(
            "Step 5: Research Depth", "Select your research depth level"
        )
    )
    selected_research_depth = select_research_depth()

    # Step 6: LLM Provider
    console.print(
        create_question_box(
            "Step 6: LLM Provider", "Select your LLM provider"
        )
    )
    selected_llm_provider, backend_url = select_llm_provider()

    # Step 7: Thinking agents
    console.print(
        create_question_box(
            "Step 7: Thinking Agents", "Select your thinking agents for analysis"
        )
    )
    selected_shallow_thinker = select_shallow_thinking_agent(selected_llm_provider)
    selected_deep_thinker = select_deep_thinking_agent(selected_llm_provider)

    # Step 8: Provider-specific thinking configuration
    thinking_level = None
    reasoning_effort = None
    anthropic_effort = None

    provider_lower = selected_llm_provider.lower()
    if provider_lower == "google":
        console.print(
            create_question_box(
                "Step 8: Thinking Mode",
                "Configure Gemini thinking mode"
            )
        )
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        console.print(
            create_question_box(
                "Step 8: Reasoning Effort",
                "Configure OpenAI reasoning effort level"
            )
        )
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        console.print(
            create_question_box(
                "Step 8: Effort Level",
                "Configure Claude effort level"
            )
        )
        anthropic_effort = ask_anthropic_effort()

    return {
        "ticker": selected_ticker,
        "analysis_date": analysis_date,
        "analysts": selected_analysts,
        "research_depth": selected_research_depth,
        "llm_provider": selected_llm_provider.lower(),
        "backend_url": backend_url,
        "shallow_thinker": selected_shallow_thinker,
        "deep_thinker": selected_deep_thinker,
        "google_thinking_level": thinking_level,
        "openai_reasoning_effort": reasoning_effort,
        "anthropic_effort": anthropic_effort,
        "output_language": output_language,
    }


def get_ticker():
    """Get ticker symbol from user input."""
    return typer.prompt("", default="SPY")


def get_analysis_date():
    """Get the analysis date from user input."""
    while True:
        date_str = typer.prompt(
            "", default=datetime.datetime.now().strftime("%Y-%m-%d")
        )
        try:
            # Validate date format and ensure it's not in the future
            analysis_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            if analysis_date.date() > datetime.datetime.now().date():
                console.print("[red]Error: Analysis date cannot be in the future[/red]")
                continue
            return date_str
        except ValueError:
            console.print(
                "[red]Error: Invalid date format. Please use YYYY-MM-DD[/red]"
            )


def save_report_to_disk(final_state, ticker: str, save_path: Path):
    """Save complete analysis report to disk with organized subfolders."""
    save_path.mkdir(parents=True, exist_ok=True)
    sections = []

    # 1. Analysts
    analysts_dir = save_path / "1_analysts"
    analyst_parts = []
    if final_state.get("market_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "market.md").write_text(final_state["market_report"])
        analyst_parts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "sentiment.md").write_text(final_state["sentiment_report"])
        analyst_parts.append(("Social Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "news.md").write_text(final_state["news_report"])
        analyst_parts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts_dir.mkdir(exist_ok=True)
        (analysts_dir / "fundamentals.md").write_text(final_state["fundamentals_report"])
        analyst_parts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        sections.append(f"## I. Analyst Team Reports\n\n{content}")

    # 2. Research
    if final_state.get("investment_debate_state"):
        research_dir = save_path / "2_research"
        debate = final_state["investment_debate_state"]
        research_parts = []
        if debate.get("bull_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bull.md").write_text(debate["bull_history"])
            research_parts.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "bear.md").write_text(debate["bear_history"])
            research_parts.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research_dir.mkdir(exist_ok=True)
            (research_dir / "manager.md").write_text(debate["judge_decision"])
            research_parts.append(("Research Manager", debate["judge_decision"]))
        if research_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in research_parts)
            sections.append(f"## II. Research Team Decision\n\n{content}")

    # 3. Trading
    if final_state.get("trader_investment_plan"):
        trading_dir = save_path / "3_trading"
        trading_dir.mkdir(exist_ok=True)
        (trading_dir / "trader.md").write_text(final_state["trader_investment_plan"])
        sections.append(f"## III. Trading Team Plan\n\n### Trader\n{final_state['trader_investment_plan']}")

    # 4. Risk Management
    if final_state.get("risk_debate_state"):
        risk_dir = save_path / "4_risk"
        risk = final_state["risk_debate_state"]
        risk_parts = []
        if risk.get("aggressive_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "aggressive.md").write_text(risk["aggressive_history"])
            risk_parts.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "conservative.md").write_text(risk["conservative_history"])
            risk_parts.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_dir.mkdir(exist_ok=True)
            (risk_dir / "neutral.md").write_text(risk["neutral_history"])
            risk_parts.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_parts:
            content = "\n\n".join(f"### {name}\n{text}" for name, text in risk_parts)
            sections.append(f"## IV. Risk Management Team Decision\n\n{content}")

        # 5. Portfolio Manager
        if risk.get("judge_decision"):
            portfolio_dir = save_path / "5_portfolio"
            portfolio_dir.mkdir(exist_ok=True)
            (portfolio_dir / "decision.md").write_text(risk["judge_decision"])
            sections.append(f"## V. Portfolio Manager Decision\n\n### Portfolio Manager\n{risk['judge_decision']}")

    # Write consolidated report
    header = f"# Trading Analysis Report: {ticker}\n\nGenerated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    (save_path / "complete_report.md").write_text(header + "\n\n".join(sections))
    return save_path / "complete_report.md"


def display_complete_report(final_state):
    """Display the complete analysis report sequentially (avoids truncation)."""
    console.print()
    console.print(Rule("Complete Analysis Report", style="bold green"))

    # I. Analyst Team Reports
    analysts = []
    if final_state.get("market_report"):
        analysts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts.append(("Social Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analysts:
        console.print(Panel("[bold]I. Analyst Team Reports[/bold]", border_style="cyan"))
        for title, content in analysts:
            console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # II. Research Team Reports
    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research = []
        if debate.get("bull_history"):
            research.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research.append(("Research Manager", debate["judge_decision"]))
        if research:
            console.print(Panel("[bold]II. Research Team Decision[/bold]", border_style="magenta"))
            for title, content in research:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # III. Trading Team
    if final_state.get("trader_investment_plan"):
        console.print(Panel("[bold]III. Trading Team Plan[/bold]", border_style="yellow"))
        console.print(Panel(Markdown(final_state["trader_investment_plan"]), title="Trader", border_style="blue", padding=(1, 2)))

    # IV. Risk Management Team
    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_reports = []
        if risk.get("aggressive_history"):
            risk_reports.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_reports.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_reports.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_reports:
            console.print(Panel("[bold]IV. Risk Management Team Decision[/bold]", border_style="red"))
            for title, content in risk_reports:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

        # V. Portfolio Manager Decision
        if risk.get("judge_decision"):
            console.print(Panel("[bold]V. Portfolio Manager Decision[/bold]", border_style="green"))
            console.print(Panel(Markdown(risk["judge_decision"]), title="Portfolio Manager", border_style="blue", padding=(1, 2)))


def update_research_team_status(status):
    """Update status for research team members (not Trader)."""
    research_team = ["Bull Researcher", "Bear Researcher", "Research Manager"]
    for agent in research_team:
        message_buffer.update_agent_status(agent, status)


# Ordered list of analysts for status transitions
ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def update_analyst_statuses(message_buffer, chunk):
    """Update analyst statuses based on accumulated report state.

    Logic:
    - Store new report content from the current chunk if present
    - Check accumulated report_sections (not just current chunk) for status
    - Analysts with reports = completed
    - First analyst without report = in_progress
    - Remaining analysts without reports = pending
    - When all analysts done, set Bull Researcher to in_progress
    """
    selected = message_buffer.selected_analysts
    found_active = False

    for analyst_key in ANALYST_ORDER:
        if analyst_key not in selected:
            continue

        agent_name = ANALYST_AGENT_NAMES[analyst_key]
        report_key = ANALYST_REPORT_MAP[analyst_key]

        # Capture new report content from current chunk
        if chunk.get(report_key):
            message_buffer.update_report_section(report_key, chunk[report_key])

        # Determine status from accumulated sections, not just current chunk
        has_report = bool(message_buffer.report_sections.get(report_key))

        if has_report:
            message_buffer.update_agent_status(agent_name, "completed")
        elif not found_active:
            message_buffer.update_agent_status(agent_name, "in_progress")
            found_active = True
        else:
            message_buffer.update_agent_status(agent_name, "pending")

    # When all analysts complete, transition research team to in_progress
    if not found_active and selected:
        if message_buffer.agent_status.get("Bull Researcher") == "pending":
            message_buffer.update_agent_status("Bull Researcher", "in_progress")

def extract_content_string(content):
    """Extract string content from various message formats.
    Returns None if no meaningful text content is found.
    """
    import ast

    def is_empty(val):
        """Check if value is empty using Python's truthiness."""
        if val is None or val == '':
            return True
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return True
            try:
                return not bool(ast.literal_eval(s))
            except (ValueError, SyntaxError):
                return False  # Can't parse = real text
        return not bool(val)

    if is_empty(content):
        return None

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, dict):
        text = content.get('text', '')
        return text.strip() if not is_empty(text) else None

    if isinstance(content, list):
        text_parts = [
            item.get('text', '').strip() if isinstance(item, dict) and item.get('type') == 'text'
            else (item.strip() if isinstance(item, str) else '')
            for item in content
        ]
        result = ' '.join(t for t in text_parts if t and not is_empty(t))
        return result if result else None

    return str(content).strip() if not is_empty(content) else None


def classify_message_type(message) -> tuple[str, str | None]:
    """Classify LangChain message into display type and extract content.

    Returns:
        (type, content) - type is one of: User, Agent, Data, Control
                        - content is extracted string or None
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    content = extract_content_string(getattr(message, 'content', None))

    if isinstance(message, HumanMessage):
        if content and content.strip() == "Continue":
            return ("Control", content)
        return ("User", content)

    if isinstance(message, ToolMessage):
        return ("Data", content)

    if isinstance(message, AIMessage):
        return ("Agent", content)

    # Fallback for unknown types
    return ("System", content)


def format_tool_args(args, max_length=80) -> str:
    """Format tool arguments for terminal display."""
    result = str(args)
    if len(result) > max_length:
        return result[:max_length - 3] + "..."
    return result

def run_analysis():
    # First get all user selections
    selections = get_user_selections()

    # Create config with selected research depth
    config = DEFAULT_CONFIG.copy()
    config["max_debate_rounds"] = selections["research_depth"]
    config["max_risk_discuss_rounds"] = selections["research_depth"]
    config["quick_think_llm"] = selections["shallow_thinker"]
    config["deep_think_llm"] = selections["deep_thinker"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    # Provider-specific thinking configuration
    config["google_thinking_level"] = selections.get("google_thinking_level")
    config["openai_reasoning_effort"] = selections.get("openai_reasoning_effort")
    config["anthropic_effort"] = selections.get("anthropic_effort")
    config["output_language"] = selections.get("output_language", "English")

    # Create stats callback handler for tracking LLM/tool calls
    stats_handler = StatsCallbackHandler()

    # Normalize analyst selection to predefined order (selection is a 'set', order is fixed)
    selected_set = {analyst.value for analyst in selections["analysts"]}
    selected_analyst_keys = [a for a in ANALYST_ORDER if a in selected_set]

    # Initialize the graph with callbacks bound to LLMs
    graph = TradingAgentsGraph(
        selected_analyst_keys,
        config=config,
        debug=True,
        callbacks=[stats_handler],
    )

    # Initialize message buffer with selected analysts
    message_buffer.init_for_analysis(selected_analyst_keys)

    # Track start time for elapsed display
    start_time = time.time()

    # Create result directory
    results_dir = Path(config["results_dir"]) / selections["ticker"] / selections["analysis_date"]
    results_dir.mkdir(parents=True, exist_ok=True)
    report_dir = results_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    log_file = results_dir / "message_tool.log"
    log_file.touch(exist_ok=True)

    def save_message_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, message_type, content = obj.messages[-1]
            content = content.replace("\n", " ")  # Replace newlines with spaces
            with open(log_file, "a") as f:
                f.write(f"{timestamp} [{message_type}] {content}\n")
        return wrapper
    
    def save_tool_call_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, tool_name, args = obj.tool_calls[-1]
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            with open(log_file, "a") as f:
                f.write(f"{timestamp} [Tool Call] {tool_name}({args_str})\n")
        return wrapper

    def save_report_section_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(section_name, content):
            func(section_name, content)
            if section_name in obj.report_sections and obj.report_sections[section_name] is not None:
                content = obj.report_sections[section_name]
                if content:
                    file_name = f"{section_name}.md"
                    text = "\n".join(str(item) for item in content) if isinstance(content, list) else content
                    with open(report_dir / file_name, "w") as f:
                        f.write(text)
        return wrapper

    message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
    message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
    message_buffer.update_report_section = save_report_section_decorator(message_buffer, "update_report_section")

    # Now start the display layout
    layout = create_layout()

    with Live(layout, refresh_per_second=4) as live:
        # Initial display
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Add initial messages
        message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
        message_buffer.add_message(
            "System", f"Analysis date: {selections['analysis_date']}"
        )
        message_buffer.add_message(
            "System",
            f"Selected analysts: {', '.join(analyst.value for analyst in selections['analysts'])}",
        )
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Update agent status to in_progress for the first analyst
        first_analyst = f"{selections['analysts'][0].value.capitalize()} Analyst"
        message_buffer.update_agent_status(first_analyst, "in_progress")
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # Create spinner text
        spinner_text = (
            f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
        )
        update_display(layout, spinner_text, stats_handler=stats_handler, start_time=start_time)

        # Initialize state and get graph args with callbacks
        init_agent_state = graph.propagator.create_initial_state(
            selections["ticker"], selections["analysis_date"]
        )
        # Pass callbacks to graph config for tool execution tracking
        # (LLM tracking is handled separately via LLM constructor)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        # Stream the analysis
        trace = []
        for chunk in graph.graph.stream(init_agent_state, **args):
            # Process all messages in chunk, deduplicating by message ID
            for message in chunk.get("messages", []):
                msg_id = getattr(message, "id", None)
                if msg_id is not None:
                    if msg_id in message_buffer._processed_message_ids:
                        continue
                    message_buffer._processed_message_ids.add(msg_id)

                msg_type, content = classify_message_type(message)
                if content and content.strip():
                    message_buffer.add_message(msg_type, content)

                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tool_call in message.tool_calls:
                        if isinstance(tool_call, dict):
                            message_buffer.add_tool_call(tool_call["name"], tool_call["args"])
                        else:
                            message_buffer.add_tool_call(tool_call.name, tool_call.args)

            # Update analyst statuses based on report state (runs on every chunk)
            update_analyst_statuses(message_buffer, chunk)

            # Research Team - Handle Investment Debate State
            if chunk.get("investment_debate_state"):
                debate_state = chunk["investment_debate_state"]
                bull_hist = debate_state.get("bull_history", "").strip()
                bear_hist = debate_state.get("bear_history", "").strip()
                judge = debate_state.get("judge_decision", "").strip()

                # Only update status when there's actual content
                if bull_hist or bear_hist:
                    update_research_team_status("in_progress")
                if bull_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bull Researcher Analysis\n{bull_hist}"
                    )
                if bear_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bear Researcher Analysis\n{bear_hist}"
                    )
                if judge:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Research Manager Decision\n{judge}"
                    )
                    update_research_team_status("completed")
                    message_buffer.update_agent_status("Trader", "in_progress")

            # Trading Team
            if chunk.get("trader_investment_plan"):
                message_buffer.update_report_section(
                    "trader_investment_plan", chunk["trader_investment_plan"]
                )
                if message_buffer.agent_status.get("Trader") != "completed":
                    message_buffer.update_agent_status("Trader", "completed")
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

            # Risk Management Team - Handle Risk Debate State
            if chunk.get("risk_debate_state"):
                risk_state = chunk["risk_debate_state"]
                agg_hist = risk_state.get("aggressive_history", "").strip()
                con_hist = risk_state.get("conservative_history", "").strip()
                neu_hist = risk_state.get("neutral_history", "").strip()
                judge = risk_state.get("judge_decision", "").strip()

                if agg_hist:
                    if message_buffer.agent_status.get("Aggressive Analyst") != "completed":
                        message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Aggressive Analyst Analysis\n{agg_hist}"
                    )
                if con_hist:
                    if message_buffer.agent_status.get("Conservative Analyst") != "completed":
                        message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Conservative Analyst Analysis\n{con_hist}"
                    )
                if neu_hist:
                    if message_buffer.agent_status.get("Neutral Analyst") != "completed":
                        message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Neutral Analyst Analysis\n{neu_hist}"
                    )
                if judge:
                    if message_buffer.agent_status.get("Portfolio Manager") != "completed":
                        message_buffer.update_agent_status("Portfolio Manager", "in_progress")
                        message_buffer.update_report_section(
                            "final_trade_decision", f"### Portfolio Manager Decision\n{judge}"
                        )
                        message_buffer.update_agent_status("Aggressive Analyst", "completed")
                        message_buffer.update_agent_status("Conservative Analyst", "completed")
                        message_buffer.update_agent_status("Neutral Analyst", "completed")
                        message_buffer.update_agent_status("Portfolio Manager", "completed")

            # Update the display
            update_display(layout, stats_handler=stats_handler, start_time=start_time)

            trace.append(chunk)

        # Get final state and decision
        final_state = trace[-1]
        decision = graph.process_signal(final_state["final_trade_decision"])

        # Update all agent statuses to completed
        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "completed")

        message_buffer.add_message(
            "System", f"Completed analysis for {selections['analysis_date']}"
        )

        # Update final report sections
        for section in message_buffer.report_sections.keys():
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        update_display(layout, stats_handler=stats_handler, start_time=start_time)

    # Post-analysis prompts (outside Live context for clean interaction)
    console.print("\n[bold cyan]Analysis Complete![/bold cyan]\n")

    # Earnings Layer — runs on top of the existing output
    earnings_brief = None
    with console.status("[bold yellow]Running Earnings Layer...[/bold yellow]"):
        try:
            earnings_layer = EarningsLayer(llm=graph.deep_thinking_llm, news_lookback_days=90)
            earnings_brief = earnings_layer.analyze(
                ticker=selections["ticker"],
                trade_date=selections["analysis_date"],
                final_state=final_state,
            )
        except Exception as e:
            console.print(f"[red]Earnings Layer error: {e}[/red]")

    if earnings_brief:
        console.print(
            Panel(
                Markdown(earnings_brief),
                title="[bold yellow]Pre-Earnings Brief[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            )
        )

    # Prompt to save report
    save_choice = typer.prompt("Save report?", default="Y").strip().upper()
    if save_choice in ("Y", "YES", ""):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = Path.cwd() / "reports" / f"{selections['ticker']}_{timestamp}"
        save_path_str = typer.prompt(
            "Save path (press Enter for default)",
            default=str(default_path)
        ).strip()
        save_path = Path(save_path_str)
        try:
            report_file = save_report_to_disk(final_state, selections["ticker"], save_path)
            console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
            console.print(f"  [dim]Complete report:[/dim] {report_file.name}")
            if earnings_brief:
                (save_path / "earnings_brief.md").write_text(earnings_brief, encoding="utf-8")
                console.print(f"  [dim]Earnings brief:[/dim] earnings_brief.md")
        except Exception as e:
            console.print(f"[red]Error saving report: {e}[/red]")

    # Prompt to display full report
    display_choice = typer.prompt("\nDisplay full report on screen?", default="Y").strip().upper()
    if display_choice in ("Y", "YES", ""):
        display_complete_report(final_state)


@app.command()
def analyze():
    run_analysis()


def _fetch_sector(ticker: str) -> str:
    """Fetch sector from yfinance, returns 'Unknown' on failure."""
    try:
        import yfinance as yf
        return yf.Ticker(ticker).info.get("sector") or "Unknown"
    except Exception:
        return "Unknown"


# ── Earnings Calendar helpers ─────────────────────────────────────────────────

def _parse_cap_str(s: str) -> "int | None":
    """Parse market cap string from Nasdaq API (e.g. '$69,241,905,992') → int."""
    if not s or s in ("N/A", "--", ""):
        return None
    s = s.strip().replace("$", "").replace(",", "")
    # Handle abbreviated forms like '69.2B' (in case format ever changes)
    multipliers = {"T": 1_000_000_000_000, "B": 1_000_000_000, "M": 1_000_000, "K": 1_000}
    for suffix, mult in multipliers.items():
        if s.upper().endswith(suffix):
            try:
                return int(float(s[:-1]) * mult)
            except ValueError:
                return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_cap_threshold(s: str) -> int:
    """Parse user-supplied cap threshold like '2B', '500M', '1.5B' → int."""
    s = s.strip().upper()
    multipliers = {"T": 1_000_000_000_000, "B": 1_000_000_000, "M": 1_000_000, "K": 1_000}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            return int(float(s[:-1]) * mult)
    return int(float(s))


def _fmt_cap(n: "int | None") -> str:
    if n is None:
        return "[dim]N/A[/dim]"
    if n >= 1_000_000_000_000:
        return f"${n/1_000_000_000_000:.1f}T"
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.0f}M"
    return f"${n:,}"


def _fmt_earnings_time(s: str) -> str:
    s = (s or "").lower()
    if "pre" in s or s == "bmo":
        return "Pre-mkt"
    if "after" in s or s in ("amc", "post"):
        return "After hrs"
    if "during" in s or s == "dmh":
        return "Intraday"
    return "?"


def _fetch_earnings_nasdaq(date_str: str) -> list:
    """Fetch earnings calendar from Nasdaq public API. No API key required."""
    import requests as _req
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }
    resp = _req.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    rows = (resp.json().get("data") or {}).get("rows") or []
    result = []
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        if not sym or sym == "N/A":
            continue
        result.append({
            "ticker":       sym,
            "company":      row.get("name", ""),
            "time":         _fmt_earnings_time(row.get("time", "")),
            "eps_estimate": row.get("epsForecast") or row.get("epsEstimate"),
            "market_cap":   _parse_cap_str(row.get("marketCap", "")),
        })
    return result


def _fetch_earnings_finnhub(date_str: str, api_key: str) -> list:
    """Fetch earnings calendar from Finnhub (API key required)."""
    import requests as _req
    url = (
        f"https://finnhub.io/api/v1/calendar/earnings"
        f"?from={date_str}&to={date_str}&token={api_key}"
    )
    resp = _req.get(url, timeout=20)
    resp.raise_for_status()
    cal = resp.json().get("earningsCalendar") or []
    result = []
    for item in cal:
        sym = (item.get("symbol") or "").strip().upper()
        if not sym:
            continue
        result.append({
            "ticker":       sym,
            "company":      "",
            "time":         _fmt_earnings_time(item.get("hour", "")),
            "eps_estimate": item.get("epsEstimate"),
            "market_cap":   None,  # enriched separately
        })
    return result


def _enrich_market_caps(entries: list) -> None:
    """Fill market_cap for entries where it is None, using yfinance fast_info."""
    import yfinance as _yf
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    missing = [e for e in entries if e["market_cap"] is None]
    if not missing:
        return

    def _fetch(entry):
        try:
            mc = _yf.Ticker(entry["ticker"]).fast_info.market_cap
            return entry["ticker"], int(mc) if mc else None
        except Exception:
            return entry["ticker"], None

    lookup: dict = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_fetch, e): e["ticker"] for e in missing}
        for fut in _as_completed(futures):
            ticker, mc = fut.result()
            lookup[ticker] = mc

    for e in entries:
        if e["ticker"] in lookup:
            e["market_cap"] = lookup[e["ticker"]]


@app.command("earnings-calendar")
def earnings_calendar(
    date:       Optional[str] = typer.Option(None,    "--date",    "-d", help="Earnings date YYYY-MM-DD (default: next weekday)"),
    source:     str           = typer.Option("nasdaq","--source",  "-s", help="Data source: nasdaq | finnhub"),
    min_cap:    Optional[str] = typer.Option(None,    "--min-cap", "-m", help="Minimum market cap e.g. 500M, 2B, 10B (prompted if omitted)"),
    all_caps:   bool          = typer.Option(False,   "--all",     "-a", help="Show all tickers regardless of market cap"),
    budget:     int           = typer.Option(100_000, "--budget",       help="Capital budget for screen run ($)"),
):
    """Fetch the earnings calendar for a date, filter by market cap, and optionally launch screening.

    Defaults to the Nasdaq public API (no API key needed). Use --source finnhub
    if you have a Finnhub API key in FINNHUB_API_KEY env var.
    """
    import os as _os
    import datetime as _dt

    console.print()
    console.print(Rule("[bold cyan]Earnings Calendar[/bold cyan]"))
    console.print()

    # ── Resolve date ──────────────────────────────────────────────────────────
    if date:
        try:
            _dt.date.fromisoformat(date)
            date_str = date
        except ValueError:
            console.print(f"[red]Invalid date format '{date}'. Use YYYY-MM-DD.[/red]")
            raise typer.Exit(1)
    else:
        # Default to next weekday
        d = _dt.date.today() + _dt.timedelta(days=1)
        while d.weekday() >= 5:  # skip Sat/Sun
            d += _dt.timedelta(days=1)
        default_date = d.isoformat()
        date_str = typer.prompt("Date (YYYY-MM-DD)", default=default_date).strip()
        try:
            _dt.date.fromisoformat(date_str)
        except ValueError:
            console.print(f"[red]Invalid date '{date_str}'.[/red]")
            raise typer.Exit(1)

    # ── Fetch calendar ────────────────────────────────────────────────────────
    source_lower = source.lower()
    with console.status(f"[cyan]Fetching earnings calendar for {date_str} from {source_lower}…[/cyan]"):
        try:
            if source_lower == "finnhub":
                api_key = _os.environ.get("FINNHUB_API_KEY", "").strip()
                if not api_key:
                    api_key = typer.prompt("Finnhub API key").strip()
                if not api_key:
                    console.print("[red]No Finnhub API key provided.[/red]")
                    raise typer.Exit(1)
                entries = _fetch_earnings_finnhub(date_str, api_key)
                console.print("[dim]Fetching market caps from yfinance (may take ~30s)…[/dim]")
                _enrich_market_caps(entries)
            else:
                entries = _fetch_earnings_nasdaq(date_str)
        except Exception as exc:
            console.print(f"[red]Failed to fetch calendar: {exc}[/red]")
            raise typer.Exit(1)

    if not entries:
        console.print(f"[yellow]No earnings found for {date_str}.[/yellow]")
        return

    # ── Interactive market cap picker (when --min-cap / --all not supplied) ──
    if not all_caps and min_cap is None:
        _CAP_TIERS = [
            ("all",   "All caps",            0),
            ("200B",  "≥ $200B  Mega cap",   200_000_000_000),
            ("10B",   "≥ $10B   Large cap",  10_000_000_000),
            ("2B",    "≥ $2B    Mid + Large", 2_000_000_000),
            ("1B",    "≥ $1B    Mid cap+",    1_000_000_000),
            ("500M",  "≥ $500M  Small+",      500_000_000),
            ("custom","Custom…",             -1),
        ]
        cap_choices = []
        for key, label, threshold in _CAP_TIERS:
            if threshold == -1:
                cap_choices.append(questionary.Choice(f"  {label}", value=key))
            else:
                count = (
                    len(entries)
                    if threshold == 0
                    else sum(1 for e in entries if (e["market_cap"] or 0) >= threshold)
                )
                cap_choices.append(
                    questionary.Choice(f"  {label:<26} ({count:>3} companies)", value=key)
                )

        cap_sel = questionary.select(
            "Minimum market cap:",
            choices=cap_choices,
            default=cap_choices[3],  # ≥$2B default
            style=questionary.Style([("highlighted", "fg:cyan bold"), ("selected", "fg:cyan")]),
        ).ask()

        if cap_sel is None:
            return
        if cap_sel == "all":
            all_caps = True
            min_cap  = "0"
        elif cap_sel == "custom":
            min_cap = typer.prompt("Enter minimum market cap (e.g. 5B, 500M)").strip()
        else:
            min_cap = cap_sel

    # ── Filter by market cap ──────────────────────────────────────────────────
    try:
        cap_threshold = 0 if all_caps else _parse_cap_threshold(min_cap or "0")
    except ValueError:
        console.print(f"[red]Cannot parse --min-cap value '{min_cap}'. Use e.g. '1B', '500M'.[/red]")
        raise typer.Exit(1)

    filtered = sorted(
        [e for e in entries if all_caps or (e["market_cap"] is not None and e["market_cap"] >= cap_threshold)],
        key=lambda e: e["market_cap"] or 0,
        reverse=True,
    )

    n_total    = len(entries)
    n_filtered = len(filtered)
    cap_label  = "all caps" if all_caps else f"market cap ≥ {min_cap}"

    console.print(Panel(
        f"[bold]Date:[/bold] {date_str}  |  [bold]Source:[/bold] {source_lower}  |  [bold]Filter:[/bold] {cap_label}\n"
        f"[bold]Total companies reporting:[/bold] {n_total}  |  [bold]Shown after filter:[/bold] {n_filtered}",
        title="[bold cyan]Results[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    if not filtered:
        console.print(
            f"[yellow]No companies with {cap_label} found for {date_str}.[/yellow]\n"
            "[dim]Try --min-cap 500M or --all to see everything.[/dim]"
        )
        return

    # ── Display table ─────────────────────────────────────────────────────────
    cal_tbl = Table(box=box.ROUNDED, show_lines=True)
    cal_tbl.add_column("#",          justify="right",   style="dim",       width=4)
    cal_tbl.add_column("Ticker",     justify="left",    style="cyan bold", width=8)
    cal_tbl.add_column("Company",    justify="left",                       width=32)
    cal_tbl.add_column("Time",       justify="center",                     width=11)
    cal_tbl.add_column("Market Cap", justify="right",                      width=12)
    cal_tbl.add_column("EPS Est.",   justify="right",                      width=10)

    for i, e in enumerate(filtered, 1):
        cap     = e["market_cap"]
        eps_str = str(e["eps_estimate"]) if e["eps_estimate"] not in (None, "N/A", "") else "[dim]N/A[/dim]"
        time_str = e["time"] or "?"

        if cap and cap >= 200_000_000_000:
            cap_color = "bold green"
        elif cap and cap >= 10_000_000_000:
            cap_color = "green"
        elif cap and cap >= 2_000_000_000:
            cap_color = "yellow"
        else:
            cap_color = "dim"

        cal_tbl.add_row(
            str(i),
            e["ticker"],
            e["company"][:31] if e["company"] else "",
            time_str,
            f"[{cap_color}]{_fmt_cap(cap)}[/{cap_color}]",
            eps_str,
        )

    console.print(cal_tbl)
    console.print()

    # ── Ticker selection ──────────────────────────────────────────────────────
    console.print(
        "[dim]Enter row numbers to screen (comma-separated), [bold]all[/bold] for all shown, "
        "or [bold]q[/bold] to quit:[/dim]"
    )
    raw = typer.prompt("").strip().lower()

    if not raw or raw == "q":
        return

    if raw == "all":
        selected = [e["ticker"] for e in filtered]
    else:
        selected = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                idx = int(part) - 1
                if 0 <= idx < len(filtered):
                    selected.append(filtered[idx]["ticker"])
                else:
                    console.print(f"[yellow]  Row {part} out of range, skipped.[/yellow]")
            except ValueError:
                console.print(f"[yellow]  '{part}' is not a valid row number, skipped.[/yellow]")

        # deduplicate, preserve order
        seen: set = set()
        selected = [t for t in selected if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]

    if not selected:
        console.print("[yellow]No valid tickers selected.[/yellow]")
        return

    console.print(f"\n[bold]Selected ({len(selected)}):[/bold] [cyan]{', '.join(selected)}[/cyan]\n")
    go = typer.prompt("Launch screen with these tickers? [Y/n]", default="Y").strip().upper()
    if go not in ("Y", "YES", ""):
        console.print("[dim]Tickers not screened. Copy them manually if needed.[/dim]")
        return

    run_screening(budget=budget, tickers_prefill=selected)


def run_screening(budget: int = 100_000, tickers_prefill: "list[str] | None" = None):
    # Raise the process FD limit before spawning any network or file I/O.
    # macOS defaults to 256 (soft); screening opens many HTTP connections and
    # cache files across tickers, exhausting that limit without this bump.
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(4096, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except Exception:
        pass

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents Earnings Screener[/bold green]\n"
        "[dim]Batch analysis + Earnings Layer across a list of tickers[/dim]",
        border_style="green",
        padding=(1, 2),
    ))

    def question_box(title, prompt):
        return Panel(f"[bold]{title}[/bold]\n[dim]{prompt}[/dim]", border_style="blue", padding=(1, 2))

    # Step 1: Tickers
    if tickers_prefill:
        tickers = tickers_prefill
        console.print(f"[green]Tickers (from earnings calendar):[/green] {', '.join(tickers)}\n")
    else:
        console.print(question_box("Step 1: Tickers", "Enter tickers separated by commas (e.g. AAPL, MSFT, NVDA)"))
        raw = typer.prompt("")
        tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
        if not tickers:
            console.print("[red]No tickers entered. Exiting.[/red]")
            return
        console.print(f"[green]Tickers:[/green] {', '.join(tickers)}\n")

    # Step 2: Analysis date
    default_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(question_box("Step 2: Analysis Date", f"Enter the analysis date (YYYY-MM-DD), default {default_date}"))
    trade_date = get_analysis_date()

    # Step 3: Research depth
    console.print(question_box("Step 3: Research Depth", "Select how deep to run the analysis for each ticker"))
    research_depth = select_research_depth()
    depth_label = {1: "Shallow", 3: "Medium", 5: "Deep"}.get(research_depth, str(research_depth))
    console.print(f"[green]Depth:[/green] {depth_label} ({research_depth} debate round(s))\n")

    # Step 4: LLM Provider
    console.print(question_box("Step 4: LLM Provider", "Select your LLM provider"))
    selected_provider, backend_url = select_llm_provider()

    # Step 5: Models
    console.print(question_box("Step 5: Quick Thinking Model", "Used for analysts and debate agents"))
    quick_model = select_shallow_thinking_agent(selected_provider)
    console.print(question_box("Step 5: Deep Thinking Model", "Used for the Earnings Layer"))
    deep_model = select_deep_thinking_agent(selected_provider)

    # Step 6: Provider-specific thinking config
    thinking_level = reasoning_effort = anthropic_effort = None
    provider_lower = selected_provider.lower()
    if provider_lower == "google":
        console.print(question_box("Step 6: Thinking Mode", "Configure Gemini thinking mode"))
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        console.print(question_box("Step 6: Reasoning Effort", "Configure OpenAI reasoning effort level"))
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        console.print(question_box("Step 6: Effort Level", "Configure Claude effort level"))
        anthropic_effort = ask_anthropic_effort()

    # Build config
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = provider_lower
    config["deep_think_llm"] = deep_model
    config["quick_think_llm"] = quick_model
    config["backend_url"] = backend_url
    config["max_debate_rounds"] = research_depth
    config["max_risk_discuss_rounds"] = research_depth
    config["google_thinking_level"] = thinking_level
    config["openai_reasoning_effort"] = reasoning_effort
    config["anthropic_effort"] = anthropic_effort

    # --- Detect available API keys for this provider ---
    import os as _os
    _PROVIDER_KEY_ENV = {
        "deepseek":   "DEEPSEEK_API_KEY",
        "xai":        "XAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "qwen":       "DASHSCOPE_API_KEY",
        "glm":        "ZHIPU_API_KEY",
        "openai":     "OPENAI_API_KEY",
        "anthropic":  "ANTHROPIC_API_KEY",
    }
    _base_env = _PROVIDER_KEY_ENV.get(provider_lower, "")
    _api_keys: list[str] = []
    if _base_env:
        for _suffix in ["", "_2", "_3", "_4", "_5", "_6", "_7", "_8"]:
            _k = _os.environ.get(_base_env + _suffix, "").strip()
            if _k and _k not in _api_keys:
                _api_keys.append(_k)

    # Resume: check for an existing screening folder for this date
    screening_dir = None
    completed_tickers: set[str] = set()
    existing_runs = sorted(
        Path("reports").glob(f"screening_{trade_date}_*/"),
        key=lambda p: p.name,
        reverse=True,
    )
    if existing_runs:
        console.print(f"\n[yellow]Found {len(existing_runs)} existing screening run(s) for {trade_date}:[/yellow]")
        for i, p in enumerate(existing_runs[:5], 1):
            done = [d.name for d in p.iterdir() if d.is_dir() and (d / "complete_report.md").exists()]
            console.print(f"  [cyan]{i}.[/cyan] {p.name}  [dim]({len(done)} completed: {', '.join(done) or 'none'})[/dim]")
        console.print("  [dim]0. Start a fresh run[/dim]")
        while True:
            choice = typer.prompt("\nResume an existing run? (enter number or 0 for fresh)", default="1").strip()
            try:
                n = int(choice)
                if n == 0:
                    break
                elif 1 <= n <= len(existing_runs[:5]):
                    screening_dir = existing_runs[n - 1]
                    completed_tickers = {
                        d.name for d in screening_dir.iterdir()
                        if d.is_dir() and (d / "complete_report.md").exists()
                    }
                    console.print(f"\n[green]Resuming:[/green] {screening_dir.name}")
                    if completed_tickers:
                        console.print(f"[dim]Skipping already completed: {', '.join(sorted(completed_tickers))}[/dim]")
                    break
                console.print(f"[red]Enter 0–{min(5, len(existing_runs))}[/red]")
            except ValueError:
                console.print("[red]Enter a number[/red]")

    if screening_dir is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        screening_dir = Path("reports") / f"screening_{trade_date}_{timestamp}"

    screening_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]Saving to:[/dim] {screening_dir.resolve()}\n")

    tickers_to_run = [t for t in tickers if t not in completed_tickers]
    if not tickers_to_run:
        console.print("[green]All tickers already completed. Nothing to do.[/green]")
        return

    # --- Step 7: Parallelism ---
    num_workers = 1
    if len(tickers_to_run) > 1:
        _n_keys = len(_api_keys)
        if _n_keys > 1:
            _step_hint = f"Found {_n_keys} {provider_lower.upper()} API keys — each worker uses a separate key"
        else:
            _step_hint = "Run multiple tickers at once using concurrent API requests"
        console.print(question_box("Step 7: Parallelism", _step_hint))

        _max_w = min(max(_n_keys, 1), 8, len(tickers_to_run))
        _par_choices = []
        for _w in range(1, _max_w + 1):
            if _w == 1:
                _label = "  1   sequential"
            elif _n_keys >= _w:
                _label = f"  {_w}   parallel  ({_w} separate API keys)"
            else:
                _label = f"  {_w}   parallel  (concurrent requests, same key)"
            _par_choices.append(questionary.Choice(_label, value=_w))
        # Always offer at least up to 3 even with a single key
        for _w in range(_max_w + 1, 4):
            if _w <= len(tickers_to_run):
                _par_choices.append(questionary.Choice(
                    f"  {_w}   parallel  (concurrent requests, same key)", value=_w
                ))

        _default_w = min(_n_keys if _n_keys > 1 else 1, len(_par_choices))
        num_workers = questionary.select(
            "Workers:",
            choices=_par_choices,
            default=_par_choices[_default_w - 1],
        ).ask() or 1
        console.print(f"[green]Workers:[/green] {num_workers}\n")

    # --- Run ---
    console.print(Rule(f"[bold cyan]Running {len(tickers_to_run)}/{len(tickers)} ticker(s) — {depth_label} — {trade_date} — {num_workers} worker(s)[/bold cyan]"))
    console.print()

    results: list[dict] = []
    results_lock = threading.Lock()

    # Seed results with any previously completed tickers so the final table
    # includes them even when resuming a partial run.
    for t in completed_tickers:
        ticker_dir = screening_dir / t
        brief_path = ticker_dir / "earnings_brief.md"
        brief_text = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
        from tradingagents.earnings.scorer import parse_score as _parse_score
        score = _parse_score(brief_text) if brief_text else {}
        results.append({
            "ticker": t,
            "sector": _fetch_sector(t),
            "ta_decision": "RESUMED",
            "brief": brief_text,
            "earnings_date": score.get("earnings_date", "unknown"),
            "beat_score": score.get("beat_score", 0),
            "guidance_score": score.get("guidance_score", 0),
            "setup_score": score.get("setup_score", 0),
            "total_score": score.get("total_score", 0),
            "signal": score.get("signal", "?"),
            "confidence": score.get("confidence", "?"),
            "one_liner": score.get("one_liner", ""),
        })

    def process(ticker: str, worker_config: dict) -> None:
        ticker_dir = screening_dir / ticker
        ticker_dir.mkdir(exist_ok=True)
        sector = _fetch_sector(ticker)
        try:
            ta = TradingAgentsGraph(debug=False, config=worker_config)
            final_state, decision = ta.propagate(ticker, trade_date)

            # Save full TradingAgents report into ticker subfolder
            save_report_to_disk(final_state, ticker, ticker_dir)

            # Run earnings layer and save brief into the same subfolder
            layer = EarningsLayer(llm=ta.deep_thinking_llm, news_lookback_days=90)
            brief, score = layer.analyze_and_score(
                ticker, trade_date, final_state, save_dir=str(ticker_dir)
            )
            result = {
                "ticker": ticker,
                "sector": sector,
                "ta_decision": decision,
                "brief": brief,
                "earnings_date": score.get("earnings_date", "unknown"),
                "beat_score": score.get("beat_score", 0),
                "guidance_score": score.get("guidance_score", 0),
                "setup_score": score.get("setup_score", 0),
                "total_score": score.get("total_score", 0),
                "signal": score.get("signal", "?"),
                "confidence": score.get("confidence", "?"),
                "one_liner": score.get("one_liner", ""),
            }
        except Exception as exc:
            result = {
                "ticker": ticker, "sector": sector, "ta_decision": "ERROR", "brief": "",
                "earnings_date": "unknown", "beat_score": 0, "guidance_score": 0,
                "setup_score": 0, "total_score": -99, "signal": "ERROR",
                "confidence": "—", "one_liner": str(exc),
            }
        with results_lock:
            results.append(result)
            n = len(results)
            sig = result.get("signal", "?")
            tot = result.get("total_score", "?")
            score_str = f"{tot:+d}" if isinstance(tot, int) else str(tot)
            console.print(
                f"  [{n}/{len(tickers)}] [cyan]{ticker}[/cyan] → "
                f"[bold]{sig}[/bold]  total: {score_str}  "
                f"[dim]saved → {ticker_dir.name}/[/dim]"
            )


    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _as_completed

    def _make_worker_config(idx: int) -> dict:
        wcfg = config.copy()
        if _api_keys:
            wcfg["api_key"] = _api_keys[idx % len(_api_keys)]
        return wcfg

    if num_workers == 1:
        for _i, _ticker in enumerate(tickers_to_run):
            process(_ticker, _make_worker_config(_i))
    else:
        with _TPE(max_workers=num_workers) as _pool:
            _futs = {
                _pool.submit(process, _ticker, _make_worker_config(_i)): _ticker
                for _i, _ticker in enumerate(tickers_to_run)
            }
            for _fut in _as_completed(_futs):
                try:
                    _fut.result()
                except Exception:
                    pass  # errors already captured and printed inside process()

    # --- Results table ---
    sorted_results = sorted(results, key=lambda r: r.get("total_score", 0), reverse=True)
    console.print()

    def sc(n: int) -> str:
        style = "green" if n > 0 else ("red" if n < 0 else "dim")
        return f"[{style}]{n:+d}[/{style}]"

    table = Table(
        box=box.ROUNDED,
        title=f"[bold]Earnings Screener — {depth_label} — {trade_date}[/bold]",
        show_lines=True,
    )
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Ticker", style="cyan bold", width=8)
    table.add_column("Sector", width=16)
    table.add_column("Earnings", width=12)
    table.add_column("Beat", justify="center", width=6)
    table.add_column("Guidance", justify="center", width=9)
    table.add_column("Setup", justify="center", width=7)
    table.add_column("Total", justify="center", width=7)
    table.add_column("Signal", justify="center", width=8)
    table.add_column("Conf.", justify="center", width=7)
    table.add_column("One-liner", no_wrap=False, min_width=30)

    for i, r in enumerate(sorted_results, 1):
        total = r.get("total_score", 0)
        signal = r.get("signal", "?")
        signal_color = {"BUY": "green", "SHORT": "red", "SKIP": "yellow"}.get(signal, "white")
        total_color = "green" if total > 0 else ("red" if total < 0 else "dim")
        table.add_row(
            str(i),
            r["ticker"],
            r.get("sector", "Unknown"),
            r.get("earnings_date", "unknown"),
            sc(r.get("beat_score", 0)),
            sc(r.get("guidance_score", 0)),
            sc(r.get("setup_score", 0)),
            f"[{total_color}]{total:+d}[/{total_color}]",
            f"[{signal_color}]{signal}[/{signal_color}]",
            r.get("confidence", "?"),
            r.get("one_liner", ""),
        )

    console.print(table)

    # --- Save ranked table ---
    table_lines = [
        f"# Earnings Screener — {depth_label} — {trade_date}\n\n",
        "| # | Ticker | Sector | Earnings | Beat | Guidance | Setup | Total | Signal | Confidence | One-liner |\n",
        "|---|--------|--------|----------|------|----------|-------|-------|--------|------------|-----------|\n",
    ]
    for i, r in enumerate(sorted_results, 1):
        table_lines.append(
            f"| {i} | {r['ticker']} | {r.get('sector','Unknown')} | {r.get('earnings_date','?')} "
            f"| {r.get('beat_score',0):+d} | {r.get('guidance_score',0):+d} "
            f"| {r.get('setup_score',0):+d} | {r.get('total_score',0):+d} "
            f"| {r.get('signal','?')} | {r.get('confidence','?')} "
            f"| {r.get('one_liner','')} |\n"
        )
    (screening_dir / "screening_table.md").write_text("".join(table_lines), encoding="utf-8")

    # --- Allocation Manager (AI Council) ---
    console.print()
    console.print(Rule("[bold magenta]Allocation Manager — AI Council[/bold magenta]"))
    allocation_report = None
    try:
        ta_alloc = TradingAgentsGraph(debug=False, config=config)
        alloc_layer = AllocationLayer(llm=ta_alloc.deep_thinking_llm, budget=budget)
        allocation_report = alloc_layer.allocate(
            results=sorted_results,
            trade_date=trade_date,
            screening_dir=screening_dir,
            save=True,
            progress_cb=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
        )
    except Exception as exc:
        console.print(f"[red]Allocation Manager error: {exc}[/red]")

    if allocation_report:
        console.print(
            Panel(
                Markdown(allocation_report),
                title=f"[bold magenta]Portfolio Allocation — ${budget:,}[/bold magenta]",
                border_style="magenta",
                padding=(1, 2),
            )
        )

        # Display allocation table from parsed JSON for quick reference
        alloc_data = parse_allocation(allocation_report)
        allocations = alloc_data.get("allocations", [])
        if allocations:
            alloc_table = Table(box=box.ROUNDED, title="[bold]Allocation Summary[/bold]", show_lines=True)
            alloc_table.add_column("Ticker", style="cyan bold", width=8)
            alloc_table.add_column("Direction", justify="center", width=10)
            alloc_table.add_column("Amount", justify="right", width=12)
            alloc_table.add_column("% Budget", justify="center", width=9)
            alloc_table.add_column("Conviction", justify="center", width=10)
            alloc_table.add_column("Rationale", no_wrap=False, min_width=30)
            for a in allocations:
                direction = a.get("direction", "SKIP")
                dir_color = {"BUY": "green", "SHORT": "red", "SKIP": "dim"}.get(direction, "white")
                amount = a.get("amount", 0)
                amount_str = f"${amount:,}" if amount else "—"
                alloc_table.add_row(
                    a.get("ticker", ""),
                    f"[{dir_color}]{direction}[/{dir_color}]",
                    f"[{dir_color}]{amount_str}[/{dir_color}]",
                    f"{a.get('pct_of_budget', 0):.1f}%",
                    a.get("conviction", ""),
                    a.get("rationale", ""),
                )
            deployed = alloc_data.get("total_deployed", 0)
            cash = alloc_data.get("cash_reserved", 0)
            console.print(alloc_table)
            console.print(
                f"  Deployed: [green]${deployed:,}[/green]  "
                f"Cash: [yellow]${cash:,}[/yellow]  "
                f"Long: [green]${alloc_data.get('long_exposure', 0):,}[/green]  "
                f"Short: [red]${alloc_data.get('short_exposure', 0):,}[/red]"
            )

    console.print(f"\n[green]✓ Results saved to:[/green] {screening_dir.resolve()}")
    console.print(f"  [dim]screening_table.md[/dim]  ← ranked table")
    if allocation_report:
        console.print(f"  [dim]allocation.md[/dim]  ← portfolio allocation")
    for r in sorted_results:
        console.print(f"  [dim]{r['ticker']}/[/dim]  ← complete_report.md + earnings_brief.md")
    _auto_build_web()


@app.command()
def screen(
    budget: int = typer.Option(100_000, "--budget", help="Capital budget for allocation manager ($)"),
):
    """Run the earnings screener across a batch of tickers."""
    run_screening(budget=budget)


def _find_analysis_for_ticker(reports_dir: Path, ticker: str) -> list[Path]:
    """Return analysis folders for the given ticker, newest first."""
    candidates = []
    if not reports_dir.exists():
        return candidates

    for folder in reports_dir.glob(f"{ticker}_*/"):
        if folder.is_dir() and (folder / "complete_report.md").exists():
            candidates.append(folder)

    for screening_dir in reports_dir.glob("screening_*/"):
        ticker_dir = screening_dir / ticker
        if ticker_dir.is_dir():
            candidates.append(ticker_dir)

    return sorted(candidates, key=lambda p: p.name, reverse=True)


def run_reflection():
    """Interactive post-mortem on a completed trade."""
    import json as _json

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents Trade Reflection[/bold green]\n"
        "[dim]Post-mortem analysis: compare predictions vs. actual outcome[/dim]",
        border_style="green",
        padding=(1, 2),
    ))

    def qbox(title, prompt, default=None):
        body = f"[bold]{title}[/bold]\n[dim]{prompt}[/dim]"
        if default:
            body += f"\n[dim]Default: {default}[/dim]"
        return Panel(body, border_style="blue", padding=(1, 2))

    # Step 1: Ticker
    console.print(qbox("Step 1: Ticker", "Enter the ticker you traded (e.g. CLS)"))
    ticker = typer.prompt("").strip().upper()
    if not ticker:
        console.print("[red]No ticker entered. Exiting.[/red]")
        return
    sector = _fetch_sector(ticker)

    # Step 2: Direction
    console.print(qbox("Step 2: Direction", "Enter BUY or SHORT"))
    while True:
        direction = typer.prompt("", default="BUY").strip().upper()
        if direction in ("BUY", "SHORT"):
            break
        console.print("[red]Please enter BUY or SHORT[/red]")

    # Step 3: Shares
    console.print(qbox("Step 3: Shares", "Number of shares traded"))
    while True:
        try:
            shares = float(typer.prompt("").strip())
            if shares > 0:
                break
            console.print("[red]Shares must be greater than 0[/red]")
        except ValueError:
            console.print("[red]Enter a valid number[/red]")

    # Step 4: Entry price
    console.print(qbox("Step 4: Entry Price", "Price per share at which you entered the trade"))
    while True:
        try:
            entry_price = float(typer.prompt("").strip())
            if entry_price > 0:
                break
            console.print("[red]Price must be greater than 0[/red]")
        except ValueError:
            console.print("[red]Enter a valid number[/red]")

    # Step 5: Exit price
    console.print(qbox("Step 5: Exit Price", "Price per share at which you closed the trade"))
    while True:
        try:
            exit_price = float(typer.prompt("").strip())
            if exit_price > 0:
                break
            console.print("[red]Price must be greater than 0[/red]")
        except ValueError:
            console.print("[red]Enter a valid number[/red]")

    # Calculate and display P&L
    pnl = (exit_price - entry_price) * shares if direction == "BUY" else (entry_price - exit_price) * shares
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if direction == "BUY" else ((entry_price - exit_price) / entry_price * 100)
    pnl_color = "green" if pnl >= 0 else "red"
    console.print(f"\n  [bold]P&L:[/bold] [{pnl_color}]${pnl:+.2f} ({pnl_pct:+.1f}%)[/{pnl_color}]\n")

    # Step 6: Trade date
    console.print(qbox("Step 6: Trade Date", "Date you entered the trade (YYYY-MM-DD)"))
    trade_date = get_analysis_date()

    # Step 7: Exit date
    default_exit = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(qbox("Step 7: Exit Date", "Date you closed the trade (YYYY-MM-DD)", default_exit))
    exit_date = get_analysis_date()

    # Step 8: Find prior analysis
    analyses = _find_analysis_for_ticker(Path("reports"), ticker)
    analysis_path = None

    if analyses:
        console.print(f"\n[green]Found {len(analyses)} prior analysis folder(s) for {ticker}:[/green]")
        for i, p in enumerate(analyses, 1):
            try:
                rel = p.relative_to(Path.cwd())
            except ValueError:
                rel = p
            has_brief = (p / "earnings_brief.md").exists()
            tag = " [dim](earnings brief)[/dim]" if has_brief else ""
            console.print(f"  [cyan]{i}.[/cyan] {rel}{tag}")
        console.print("  [dim]0. Skip (no prior analysis)[/dim]")

        while True:
            choice = typer.prompt("\nSelect analysis to use", default="1").strip()
            try:
                n = int(choice)
                if n == 0:
                    analysis_path = None
                    break
                elif 1 <= n <= len(analyses):
                    analysis_path = analyses[n - 1]
                    break
                console.print(f"[red]Enter a number between 0 and {len(analyses)}[/red]")
            except ValueError:
                console.print("[red]Enter a number[/red]")
    else:
        console.print(f"\n[yellow]No prior analysis found for {ticker} in reports/[/yellow]")
        console.print("[dim]The post-mortem will proceed without prior analysis context.[/dim]")

    console.print()

    # Step 9: LLM Provider
    console.print(qbox("Step 9: LLM Provider", "Select the LLM provider for the post-mortem"))
    selected_provider, backend_url = select_llm_provider()

    console.print(qbox("Step 9b: Model", "Select the model for the post-mortem analysis"))
    deep_model = select_deep_thinking_agent(selected_provider)

    thinking_level = reasoning_effort = anthropic_effort = None
    provider_lower = selected_provider.lower()
    if provider_lower == "google":
        console.print(qbox("Step 10: Thinking Mode", "Configure Gemini thinking mode"))
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        console.print(qbox("Step 10: Reasoning Effort", "Configure OpenAI reasoning effort"))
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        console.print(qbox("Step 10: Effort Level", "Configure Claude effort level"))
        anthropic_effort = ask_anthropic_effort()

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = provider_lower
    config["deep_think_llm"] = deep_model
    config["quick_think_llm"] = deep_model
    config["backend_url"] = backend_url
    config["google_thinking_level"] = thinking_level
    config["openai_reasoning_effort"] = reasoning_effort
    config["anthropic_effort"] = anthropic_effort

    # Run reflection
    console.print()
    console.print(Rule(f"[bold cyan]Running Post-Mortem: {ticker} ({direction})[/bold cyan]"))
    console.print()

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = Path("reports") / "reflections" / f"{ticker}_{trade_date}_{timestamp}"

    post_mortem = None
    with console.status("[bold yellow]Fetching post-trade data and generating post-mortem...[/bold yellow]"):
        try:
            ta = TradingAgentsGraph(debug=False, config=config)
            layer = ReflectionLayer(llm=ta.deep_thinking_llm)
            post_mortem = layer.analyze(
                ticker=ticker,
                trade_date=trade_date,
                exit_date=exit_date,
                direction=direction,
                shares=shares,
                entry_price=entry_price,
                exit_price=exit_price,
                prior_analysis_path=str(analysis_path) if analysis_path else None,
                save_dir=str(save_dir),
            )
        except Exception as exc:
            console.print(f"[red]Reflection error: {exc}[/red]")
            return

    if post_mortem:
        console.print(
            Panel(
                Markdown(post_mortem),
                title=f"[bold yellow]Trade Post-Mortem: {ticker}[/bold yellow]",
                border_style="yellow",
                padding=(1, 2),
            )
        )

    # Parse score for the trade log
    score = parse_reflection_score(post_mortem) if post_mortem else {}

    # Append to trade log
    trade_log_path = Path.home() / ".tradingagents" / "trades.json"
    trade_log_path.parent.mkdir(parents=True, exist_ok=True)

    existing_trades = []
    if trade_log_path.exists():
        try:
            existing_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        except Exception:
            existing_trades = []

    # Detect if the analysis came from a screening run
    screening_run = None
    if analysis_path:
        for parent in Path(analysis_path).parents:
            if parent.name.startswith("screening_"):
                screening_run = str(parent)
                break

    trade_entry = {
        "ticker": ticker,
        "sector": sector,
        "direction": direction,
        "shares": shares,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "outcome": score.get("outcome", "UNKNOWN"),
        "prediction_accuracy": score.get("prediction_accuracy", "UNKNOWN"),
        "beat_prediction_correct": score.get("beat_prediction_correct"),
        "guidance_prediction_correct": score.get("guidance_prediction_correct"),
        "key_lesson": score.get("key_lesson", ""),
        "trade_date": trade_date,
        "exit_date": exit_date,
        "screening_run": screening_run,
        "analysis_path": str(analysis_path) if analysis_path else None,
        "reflection_path": str(save_dir),
        "logged_at": datetime.datetime.now().isoformat(),
    }
    existing_trades.append(trade_entry)
    trade_log_path.write_text(_json.dumps(existing_trades, indent=2), encoding="utf-8")

    console.print(f"\n[green]✓ Post-mortem saved to:[/green] {save_dir.resolve()}")
    console.print(f"[green]✓ Trade logged to:[/green] {trade_log_path}")


def _find_existing_reflection(reports_dir: Path, ticker: str, exit_date: str) -> "Path | None":
    """Scan reports/reflections/ for the most recent folder matching TICKER_EXITDATE_*."""
    reflections_dir = reports_dir / "reflections"
    if not reflections_dir.exists():
        return None
    prefix = f"{ticker}_{exit_date}_"
    matches = sorted(
        (d for d in reflections_dir.iterdir() if d.is_dir() and d.name.startswith(prefix)),
        reverse=True,
    )
    return matches[0] if matches else None


def _consolidate_for_reflect(indexed: list, reports_dir: Path) -> list:
    """Merge fills with the same ticker+exit_date into single consolidated entries.

    Returns a list of group dicts sorted by exit_date desc. Each group has:
      ticker, exit_date, direction, sector, pnl, pnl_pct, shares,
      entry_price, exit_price, outcome, fills, orig_indices,
      analysis_path, trade_date, key_lesson,
      reflected (bool), existing_reflection_path (Path|None)
    """
    from collections import OrderedDict as _OD

    trade_map = {orig_idx: t for orig_idx, t in indexed}
    groups: dict = _OD()

    for orig_idx, t in indexed:
        key = (t.get("ticker", ""), t.get("exit_date", ""))
        if key not in groups:
            groups[key] = {
                "ticker":        t.get("ticker", ""),
                "exit_date":     t.get("exit_date", ""),
                "direction":     t.get("direction", "BUY"),
                "sector":        t.get("sector", ""),
                "pnl":           0.0,
                "shares":        0.0,
                "_entry_wtd":    0.0,
                "_exit_wtd":     0.0,
                "fills":         0,
                "orig_indices":  [],
                "analysis_path": None,
                "trade_date":    None,
                "key_lesson":    None,
            }
        g = groups[key]
        sh = t.get("shares") or 0
        g["pnl"]         += t.get("pnl") or 0
        g["shares"]      += sh
        g["_entry_wtd"]  += (t.get("entry_price") or 0) * sh
        g["_exit_wtd"]   += (t.get("exit_price")  or 0) * sh
        g["fills"]       += 1
        g["orig_indices"].append(orig_idx)
        if not g["analysis_path"] and t.get("analysis_path"):
            g["analysis_path"] = t["analysis_path"]
        if not g["trade_date"] and t.get("trade_date"):
            g["trade_date"] = t["trade_date"]
        if not g["key_lesson"] and t.get("key_lesson"):
            g["key_lesson"] = t["key_lesson"]

    result = []
    for g in groups.values():
        sh          = g["shares"]
        entry_price = g["_entry_wtd"] / sh if sh else 0.0
        exit_price  = g["_exit_wtd"]  / sh if sh else 0.0
        cost        = entry_price * sh
        pnl_pct     = g["pnl"] / cost * 100 if cost else 0.0
        outcome     = "WIN" if g["pnl"] > 0.005 else "LOSS" if g["pnl"] < -0.005 else "BREAK_EVEN"
        ticker      = g["ticker"]
        exit_date   = g["exit_date"]

        # Check stored reflection_path on any fill first
        existing_rp: "Path | None" = None
        for idx in g["orig_indices"]:
            rp = trade_map[idx].get("reflection_path")
            if rp and Path(rp).exists():
                existing_rp = Path(rp)
                break
        # Fall back to filesystem scan (catches reflections run before path was stored)
        if existing_rp is None:
            existing_rp = _find_existing_reflection(reports_dir, ticker, exit_date)

        del g["_entry_wtd"], g["_exit_wtd"]
        result.append({
            **g,
            "entry_price":              entry_price,
            "exit_price":               exit_price,
            "pnl_pct":                  pnl_pct,
            "outcome":                  outcome,
            "reflected":                existing_rp is not None,
            "existing_reflection_path": existing_rp,
        })
    return result


def _trade_alloc_score(trade: dict) -> str:
    """Return the total_score from the earnings brief, or '—' if unavailable."""
    ap = trade.get("analysis_path")
    if not ap:
        return "—"
    brief = Path(ap) / "earnings_brief.md"
    if not brief.exists():
        return "—"
    import re as _re2
    m = _re2.search(r'"total_score"\s*:\s*(-?\d+)', brief.read_text(encoding="utf-8"))
    return f"{int(m.group(1)):+d}" if m else "—"


@app.command()
def reflect():
    """Run a post-mortem on a trade from your history.

    Shows all trades with P&L and reflection status, lets you pick one,
    then runs the post-mortem using data already in trades.json — no
    manual re-entry needed. Fills for the same ticker and day are merged.
    """
    import json as _json

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents — Trade Reflection[/bold green]\n"
        "[dim]Select a trade to reflect on. Data is pre-filled from your trade history.[/dim]",
        border_style="green", padding=(1, 2),
    ))

    trade_log_path = Path.home() / ".tradingagents" / "trades.json"
    if not trade_log_path.exists():
        console.print("[yellow]No trades found. Import trades first with 'tradingagents import-ibkr'.[/yellow]")
        return

    try:
        all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
    except Exception:
        all_trades = []

    if not all_trades:
        console.print("[yellow]trades.json is empty.[/yellow]")
        return

    reports_dir = Path("reports")

    # Consolidate fills: same ticker + exit_date → one entry
    all_indexed = sorted(enumerate(all_trades), key=lambda x: x[1].get("exit_date", ""), reverse=True)
    groups = _consolidate_for_reflect(all_indexed, reports_dir)

    # ── Filter: pending or all ─────────────────────────────────────────────
    show_mode = questionary.select(
        "Show trades:",
        choices=["Pending reflection only", "All trades"],
    ).ask()
    if show_mode is None:
        return
    if show_mode == "Pending reflection only":
        groups = [g for g in groups if not g["reflected"]]
        if not groups:
            console.print("[green]All trades already have a reflection.[/green]")
            return

    # ── Optional ticker search ─────────────────────────────────────────────
    search = questionary.text("Filter by ticker (leave blank for all):").ask()
    if search is None:
        return
    if search.strip():
        q = search.strip().upper()
        groups = [g for g in groups if q in g["ticker"].upper()]
        if not groups:
            console.print(f"[yellow]No trades found matching '{q}'.[/yellow]")
            return

    # ── Picker table ───────────────────────────────────────────────────────
    total_fills = sum(g["fills"] for g in groups)
    title_str = f"[bold]Trade History — {len(groups)} trade{'s' if len(groups) != 1 else ''}"
    if total_fills != len(groups):
        title_str += f" · {total_fills} fills"
    title_str += "[/bold]"

    tbl = Table(box=box.ROUNDED, title=title_str, show_lines=True)
    tbl.add_column("#",         justify="right",  style="dim", width=4)
    tbl.add_column("Ticker",    style="cyan bold",             width=8)
    tbl.add_column("Dir",       justify="center",              width=6)
    tbl.add_column("Exit Date",                                width=12)
    tbl.add_column("P&L",       justify="right",               width=11)
    tbl.add_column("P&L %",     justify="right",               width=8)
    tbl.add_column("Outcome",   justify="center",              width=10)
    tbl.add_column("Score",     justify="center",              width=7)
    tbl.add_column("Fills",     justify="center",              width=6)
    tbl.add_column("Sector",    style="dim",                   width=14)
    tbl.add_column("Reflected", justify="center",              width=10)

    for n, g in enumerate(groups, 1):
        pnl     = g["pnl"]
        pnl_pct = g["pnl_pct"]
        outcome = g["outcome"]
        pnl_col = "green" if pnl >= 0 else "red"
        out_col = {"WIN": "green", "LOSS": "red", "BREAK_EVEN": "dim"}.get(outcome, "dim")
        dir_col = {"BUY": "green", "SHORT": "red"}.get(g["direction"], "white")
        reflected_cell = "[green]✓[/green]" if g["reflected"] else "[dim]—[/dim]"
        fills_cell = f"[dim]×{g['fills']}[/dim]" if g["fills"] > 1 else "[dim]—[/dim]"
        tbl.add_row(
            str(n),
            g["ticker"],
            f"[{dir_col}]{g['direction']}[/{dir_col}]",
            g["exit_date"],
            f"[{pnl_col}]${pnl:+.2f}[/{pnl_col}]",
            f"[{pnl_col}]{pnl_pct:+.1f}%[/{pnl_col}]",
            f"[{out_col}]{outcome}[/{out_col}]",
            _trade_alloc_score(g),
            fills_cell,
            g["sector"] or "—",
            reflected_cell,
        )

    console.print(tbl)

    # ── Select trade(s) — comma-separated for batch ────────────────────────
    raw = questionary.text(
        f"Enter number(s) 1–{len(groups)}, comma-separated, or 'q' to quit:"
    ).ask()
    if not raw or raw.strip().lower() == "q":
        return

    picks: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if 1 <= n <= len(groups):
                picks.append(n)
            else:
                console.print(f"[red]{n} is out of range (1–{len(groups)}), skipping.[/red]")
        except ValueError:
            console.print(f"[red]'{part}' is not a valid number, skipping.[/red]")

    # Deduplicate while preserving order
    seen: set[int] = set()
    picks = [p for p in picks if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]

    if not picks:
        console.print("[red]No valid selections.[/red]")
        return

    selected_groups = [groups[p - 1] for p in picks]

    # ── LLM provider — selected once for all reflections ───────────────────
    def qbox(title, prompt):
        return Panel(f"[bold]{title}[/bold]\n[dim]{prompt}[/dim]", border_style="blue", padding=(1, 2))

    suffix = f" ({len(selected_groups)} trades)" if len(selected_groups) > 1 else ""
    console.print(qbox("LLM Provider", f"Select the provider for the post-mortem{suffix}"))
    selected_provider, backend_url = select_llm_provider()
    console.print(qbox("Model", f"Select the model for the post-mortem{suffix}"))
    deep_model = select_deep_thinking_agent(selected_provider)

    thinking_level = reasoning_effort = anthropic_effort = None
    provider_lower = selected_provider.lower()
    if provider_lower == "google":
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        anthropic_effort = ask_anthropic_effort()

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"]            = provider_lower
    config["deep_think_llm"]          = deep_model
    config["quick_think_llm"]         = deep_model
    config["backend_url"]             = backend_url
    config["google_thinking_level"]   = thinking_level
    config["openai_reasoning_effort"] = reasoning_effort
    config["anthropic_effort"]        = anthropic_effort

    # Initialise LLM once; reuse across all trades in the batch
    try:
        ta    = TradingAgentsGraph(debug=False, config=config)
        layer = ReflectionLayer(llm=ta.deep_thinking_llm)
    except Exception as exc:
        console.print(f"[red]Failed to initialise LLM: {exc}[/red]")
        return

    # ── Process each selected trade ────────────────────────────────────────
    batch_results: list[tuple[str, str, str, "Path | None"]] = []  # (ticker, date, status, path)

    for batch_n, group in enumerate(selected_groups, 1):
        if len(selected_groups) > 1:
            console.print()
            console.print(Rule(f"[dim]Trade {batch_n} of {len(selected_groups)}[/dim]"))

        ticker       = group["ticker"]
        direction    = group["direction"]
        shares       = group["shares"]
        entry_price  = group["entry_price"]
        exit_price   = group["exit_price"]
        pnl          = group["pnl"]
        pnl_pct      = group["pnl_pct"]
        exit_date    = group["exit_date"]
        trade_date   = group.get("trade_date") or exit_date
        orig_indices = group["orig_indices"]

        # Confirm
        pnl_col = "green" if pnl >= 0 else "red"
        dir_col = {"BUY": "green", "SHORT": "red"}.get(direction, "white")
        fills_note = f"  Fills:     {group['fills']} partial orders\n" if group["fills"] > 1 else ""
        console.print()
        console.print(Panel(
            f"  Ticker:    [cyan bold]{ticker}[/cyan bold]\n"
            f"  Direction: [{dir_col}]{direction}[/{dir_col}]\n"
            f"  Shares:    {shares:,.0f}\n"
            f"{fills_note}"
            f"  Entry:    ${entry_price:.2f}  →  Exit: ${exit_price:.2f}\n"
            f"  P&L:      [{pnl_col}]${pnl:+.2f} ({pnl_pct:+.1f}%)[/{pnl_col}]\n"
            f"  Exit date: {exit_date}",
            title=f"[bold]Reflecting on: {ticker}[/bold]",
            border_style="cyan", padding=(1, 2),
        ))

        if group["reflected"]:
            existing = group["existing_reflection_path"]
            console.print(f"[yellow]A reflection already exists:[/yellow] [dim]{existing}[/dim]")
            if not questionary.confirm("Overwrite?", default=False).ask():
                batch_results.append((ticker, exit_date, "skipped", None))
                continue

        # Find prior analysis
        analysis_path = None
        stored_ap = group.get("analysis_path")
        if stored_ap and Path(stored_ap).exists():
            analysis_path = Path(stored_ap)
            console.print(f"\n[dim]Using stored analysis: {analysis_path.name}[/dim]")
        else:
            analyses = _find_analysis_for_ticker(reports_dir, ticker)
            if analyses:
                console.print(f"\n[green]Found {len(analyses)} analysis folder(s) for {ticker}:[/green]")
                for i, p in enumerate(analyses, 1):
                    tag = " [dim](earnings brief)[/dim]" if (p / "earnings_brief.md").exists() else ""
                    console.print(f"  [cyan]{i}.[/cyan] {p.name}{tag}")
                console.print("  [dim]0. Skip[/dim]")
                while True:
                    c = typer.prompt("Select analysis", default="1").strip()
                    try:
                        n = int(c)
                        if n == 0:
                            break
                        if 1 <= n <= len(analyses):
                            analysis_path = analyses[n - 1]
                            break
                        console.print(f"[red]Enter 0–{len(analyses)}[/red]")
                    except ValueError:
                        console.print("[red]Enter a number[/red]")
            else:
                console.print(f"\n[yellow]No prior analysis found for {ticker}. Proceeding without it.[/yellow]")

        console.print()
        console.print(Rule(f"[bold cyan]Post-Mortem: {ticker} ({direction})[/bold cyan]"))
        console.print()

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir  = Path("reports") / "reflections" / f"{ticker}_{exit_date}_{timestamp}"

        post_mortem = None
        with console.status(f"[bold yellow]Generating post-mortem for {ticker}...[/bold yellow]"):
            try:
                post_mortem = layer.analyze(
                    ticker=ticker,
                    trade_date=trade_date,
                    exit_date=exit_date,
                    direction=direction,
                    shares=shares,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    prior_analysis_path=str(analysis_path) if analysis_path else None,
                    save_dir=str(save_dir),
                )
            except Exception as exc:
                console.print(f"[red]Reflection error for {ticker}: {exc}[/red]")
                batch_results.append((ticker, exit_date, "error", None))
                continue

        if post_mortem:
            console.print(Panel(
                Markdown(post_mortem),
                title=f"[bold yellow]Trade Post-Mortem: {ticker}[/bold yellow]",
                border_style="yellow", padding=(1, 2),
            ))

        # Update all fill entries in trades.json
        score = parse_reflection_score(post_mortem) if post_mortem else {}

        screening_run = None
        if analysis_path:
            for parent in Path(analysis_path).parents:
                if parent.name.startswith("screening_"):
                    screening_run = str(parent)
                    break
        if not screening_run:
            for idx in orig_indices:
                sr = all_trades[idx].get("screening_run")
                if sr:
                    screening_run = sr
                    break

        update = {
            "beat_prediction_correct":     score.get("beat_prediction_correct"),
            "guidance_prediction_correct": score.get("guidance_prediction_correct"),
            "key_lesson":                  score.get("key_lesson", ""),
            "outcome":                     score.get("outcome") or group["outcome"],
            "prediction_accuracy":         score.get("prediction_accuracy", "UNKNOWN"),
            "screening_run":               screening_run,
            "analysis_path":               str(analysis_path) if analysis_path else group.get("analysis_path"),
            "reflection_path":             str(save_dir),
            "reflected_at":                datetime.datetime.now().isoformat(),
        }
        for idx in orig_indices:
            all_trades[idx].update(update)

        trade_log_path.write_text(_json.dumps(all_trades, indent=2), encoding="utf-8")

        console.print(f"\n[green]✓ Saved:[/green] {save_dir.resolve()}")
        if len(orig_indices) > 1:
            console.print(f"[dim]  ({len(orig_indices)} fill records updated)[/dim]")

        batch_results.append((ticker, exit_date, "done", save_dir))

    # ── Batch summary (shown only when more than one trade was selected) ────
    if len(selected_groups) > 1:
        console.print()
        console.print(Rule("[bold]Reflection Summary[/bold]"))
        for t, d, status, path in batch_results:
            if status == "done":
                console.print(f"  [green]✓[/green]  {t}  {d}")
            elif status == "skipped":
                console.print(f"  [yellow]—[/yellow]  {t}  {d}  [dim]skipped[/dim]")
            else:
                console.print(f"  [red]✗[/red]  {t}  {d}  [dim]error[/dim]")
        done = sum(1 for _, _, s, _ in batch_results if s == "done")
        console.print(f"\n[dim]{done} of {len(batch_results)} completed · trades.json updated[/dim]")
    _auto_build_web()


def _scan_reflection_folders(reports_dir: Path, all_trades: list) -> list[dict]:
    """Return one entry per unique ticker+exit_date (most-recent run wins for duplicates).

    Each entry: ticker, exit_date, direction, pnl, pnl_pct, outcome,
                beat_correct, guide_correct, key_lesson, content, folder.
    """
    reflections_dir = reports_dir / "reflections"
    if not reflections_dir.exists():
        return []

    # trades.json lookup: (ticker, exit_date) → most-recent reflected trade
    trade_lookup: dict = {}
    for t in all_trades:
        key = (t.get("ticker", ""), t.get("exit_date", ""))
        prev = trade_lookup.get(key)
        if prev is None or (t.get("reflected_at", "") or "") > (prev.get("reflected_at", "") or ""):
            trade_lookup[key] = t

    # Collect all valid folders sorted newest first
    folders: list[tuple[str, Path]] = []
    for d in reflections_dir.iterdir():
        if not d.is_dir():
            continue
        pm = d / "post_mortem.md"
        if not pm.exists():
            continue
        parts = d.name.split("_")
        if len(parts) < 3:
            continue
        ticker    = parts[0]
        exit_date = parts[1]
        timestamp = "_".join(parts[2:])
        folders.append((timestamp, d, ticker, exit_date))
    folders.sort(key=lambda x: x[0], reverse=True)  # newest first

    # Deduplicate: keep most-recent run per (ticker, exit_date)
    seen: set = set()
    items: list[dict] = []
    for timestamp, d, ticker, exit_date in folders:
        key = (ticker, exit_date)
        if key in seen:
            continue
        seen.add(key)
        content = (d / "post_mortem.md").read_text(encoding="utf-8")
        score   = parse_reflection_score(content)
        trade   = trade_lookup.get(key, {})
        items.append({
            "ticker":       ticker,
            "exit_date":    exit_date,
            "timestamp":    timestamp,
            "direction":    score.get("direction") or trade.get("direction", "?"),
            "pnl":          trade.get("pnl"),
            "pnl_pct":      score.get("pnl_pct") if score.get("pnl_pct") is not None else trade.get("pnl_pct"),
            "outcome":      score.get("outcome") or trade.get("outcome", "?"),
            "beat_correct": score.get("beat_prediction_correct"),
            "guide_correct": score.get("guidance_prediction_correct"),
            "key_lesson":   score.get("key_lesson", ""),
            "content":      content,
            "folder":       d,
        })

    # Re-sort by exit_date desc
    items.sort(key=lambda x: x["exit_date"], reverse=True)
    return items


def _build_improve_prompt(items: list[dict]) -> str:
    """Build the structured LLM prompt from a list of reflection items."""
    n = len(items)
    wins   = sum(1 for i in items if i["outcome"] == "WIN")
    losses = sum(1 for i in items if i["outcome"] == "LOSS")

    beat_all   = [i for i in items if i["beat_correct"]  is not None]
    guide_all  = [i for i in items if i["guide_correct"] is not None]
    beat_acc   = f"{sum(1 for i in beat_all  if i['beat_correct'])}/{len(beat_all)}"   if beat_all  else "N/A"
    guide_acc  = f"{sum(1 for i in guide_all if i['guide_correct'])}/{len(guide_all)}" if guide_all else "N/A"

    pnl_pcts  = [i["pnl_pct"] for i in items if i["pnl_pct"] is not None]
    avg_pct   = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0

    lines = [
        "# Trade Reflection Analysis — System Improvement Request",
        "",
        "## Context: What TradingAgents Does",
        "",
        "TradingAgents is a pre-earnings research and automated allocation framework:",
        "- **Pipeline**: 5 LangGraph teams in sequence — market analyst, fundamentals analyst,",
        "  news analyst, social analyst → bull/bear researchers → research manager →",
        "  risk management (aggressive/conservative/neutral) → portfolio manager (BUY/SHORT/SKIP)",
        "- **Scoring**: each ticker receives three scores (-5 to +5):",
        "  - `beat_score`: EPS beat likelihood",
        "  - `guidance_score`: forward guidance tone",
        "  - `setup_score`: technical/fundamental pre-earnings setup",
        "  - `weighted_score = beat_w × beat_score + guidance_w × guidance_score + setup_w × setup_score`",
        "- **Allocation**: an AI Council (5 advisors → cross-review → synthesis) sizes positions",
        "  using the weighted_score; high conviction = 15–25%, medium = 7–14%, low ≤ 6% or SKIP",
        "",
        "## Batch Statistics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Reflections | {n} |",
        f"| Win / Loss | {wins} / {losses} ({wins/n*100:.0f}% win rate) |",
        f"| Avg P&L % | {avg_pct:+.1f}% |",
        f"| Beat prediction accuracy | {beat_acc} |",
        f"| Guidance prediction accuracy | {guide_acc} |",
        "",
        "## Individual Post-Mortems",
        "",
    ]

    for idx, item in enumerate(items, 1):
        outcome_tag = f"{item['direction']}, {item['outcome']}"
        lines.append(f"### {idx}. {item['ticker']} — {item['exit_date']} ({outcome_tag})")
        lines.append("")
        lines.append(item["content"].strip())
        lines.append("")
        lines.append("---")
        lines.append("")

    lines += [
        "## What I Need From You",
        "",
        "Analyse the post-mortems above and produce a structured improvement report.",
        "Be **specific and actionable**. Reference individual trades where relevant",
        "(e.g. 'As in the ANET post-mortem, ...'). Format your output in markdown.",
        "",
        "### 1. Failure Patterns",
        "The 3–5 most common failure modes. For each: what went wrong, which part of the",
        "pipeline caused it (analyst prompt / scorer / council / risk mgmt / PM), and how",
        "often it appeared across the batch.",
        "",
        "### 2. Success Patterns",
        "What the system got right when it worked. Which signals or reasoning steps were",
        "most reliable predictors of a winning trade.",
        "",
        "### 3. Missing Data / Blind Spots",
        "Information that was absent from the pre-earnings brief but would have changed",
        "the outcome. For each gap, name the specific data source or calculation to add",
        "(e.g. 'options-implied expected move from yfinance/CBOE', 'historical guidance",
        "surprise rate per company from earnings-whispers').",
        "",
        "### 4. Prompt Improvements",
        "For each relevant agent role, a specific change. Use this format:",
        "",
        "**[Role]**",
        "- Current weakness: ...",
        "- Proposed change: ...",
        "- Expected impact: ...",
        "",
        "Roles to address: Market Analyst, Fundamentals Analyst, Bull/Bear Researchers,",
        "Research Manager, Portfolio Manager, AI Council synthesis prompt.",
        "",
        "### 5. Scoring Weight Adjustments",
        "Based on the beat/guidance accuracy statistics and the per-trade post-mortems,",
        "recommend specific numeric weights for beat_w, guidance_w, setup_w.",
        "Show your reasoning (e.g. guidance was correct only 2/8 times → reduce guidance_w).",
        "",
        "### 6. Process / Structural Changes",
        "Any new pipeline steps, new agents, threshold changes, or workflow adjustments",
        "that would structurally improve outcomes — beyond prompt tweaks.",
    ]

    return "\n".join(lines)


@app.command()
def improve():
    """Analyse trade reflections with an LLM and generate system-improvement suggestions.

    Scans all post-mortems, lets you choose which to include, submits them
    to the chosen LLM, and saves the output to reports/improvement_TIMESTAMP.md.
    """
    import json as _json
    from langchain_core.messages import HumanMessage, SystemMessage

    console.print()
    console.print(Panel(
        "[bold green]TradingAgents — System Improvement Analysis[/bold green]\n"
        "[dim]Synthesise trade reflections into actionable pipeline improvements.[/dim]",
        border_style="green", padding=(1, 2),
    ))

    reports_dir    = Path("reports")
    trade_log_path = Path.home() / ".tradingagents" / "trades.json"
    all_trades: list = []
    if trade_log_path.exists():
        try:
            all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    items = _scan_reflection_folders(reports_dir, all_trades)
    if not items:
        console.print("[yellow]No reflections found in reports/reflections/.[/yellow]")
        return

    # ── Picker table ───────────────────────────────────────────────────────
    tbl = Table(
        box=box.ROUNDED,
        title=f"[bold]Available Reflections — {len(items)} trade{'s' if len(items) != 1 else ''}[/bold]",
        show_lines=True,
    )
    tbl.add_column("#",          justify="right", style="dim",  width=4)
    tbl.add_column("Ticker",     style="cyan bold",             width=8)
    tbl.add_column("Exit Date",                                 width=12)
    tbl.add_column("Dir",        justify="center",              width=6)
    tbl.add_column("P&L %",      justify="right",               width=8)
    tbl.add_column("Outcome",    justify="center",              width=10)
    tbl.add_column("Beat ✓",     justify="center",              width=8)
    tbl.add_column("Guide ✓",    justify="center",              width=8)
    tbl.add_column("Key Lesson", style="dim",                   width=42)

    def _bool_cell(v: "bool | None") -> str:
        if v is True:  return "[green]✓[/green]"
        if v is False: return "[red]✗[/red]"
        return "[dim]—[/dim]"

    for n, item in enumerate(items, 1):
        pp      = item["pnl_pct"]
        outcome = item["outcome"]
        pnl_col = "green" if (pp or 0) >= 0 else "red"
        out_col = {"WIN": "green", "LOSS": "red", "BREAK_EVEN": "dim"}.get(outcome, "dim")
        dir_col = {"BUY": "green", "SHORT": "red"}.get(item["direction"], "white")
        lesson  = (item["key_lesson"] or "")
        lesson  = lesson[:55] + ("…" if len(lesson) > 55 else "")
        tbl.add_row(
            str(n),
            item["ticker"],
            item["exit_date"],
            f"[{dir_col}]{item['direction']}[/{dir_col}]",
            f"[{pnl_col}]{pp:+.1f}%[/{pnl_col}]" if pp is not None else "—",
            f"[{out_col}]{outcome}[/{out_col}]",
            _bool_cell(item["beat_correct"]),
            _bool_cell(item["guide_correct"]),
            lesson,
        )

    console.print(tbl)

    # ── Selection ──────────────────────────────────────────────────────────
    raw = questionary.text(
        f"Enter 'all', comma-separated numbers (1–{len(items)}), or 'q' to quit:"
    ).ask()
    if not raw or raw.strip().lower() == "q":
        return

    if raw.strip().lower() == "all":
        selected = list(items)
    else:
        picks: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
                if 1 <= n <= len(items):
                    picks.append(n)
                else:
                    console.print(f"[red]{n} out of range, skipping.[/red]")
            except ValueError:
                console.print(f"[red]'{part}' is not a valid number, skipping.[/red]")
        seen_picks: set[int] = set()
        picks = [p for p in picks if not (p in seen_picks or seen_picks.add(p))]  # type: ignore[func-returns-value]
        if not picks:
            console.print("[red]No valid selections.[/red]")
            return
        selected = [items[p - 1] for p in picks]

    console.print(f"\n[dim]Using {len(selected)} reflection(s) for analysis.[/dim]")

    # ── LLM provider ───────────────────────────────────────────────────────
    def qbox(title, prompt):
        return Panel(f"[bold]{title}[/bold]\n[dim]{prompt}[/dim]", border_style="blue", padding=(1, 2))

    console.print(qbox("LLM Provider", "Select the provider for the improvement analysis"))
    selected_provider, backend_url = select_llm_provider()
    console.print(qbox("Model", "Select the model for the improvement analysis"))
    deep_model = select_deep_thinking_agent(selected_provider)

    thinking_level = reasoning_effort = anthropic_effort = None
    provider_lower = selected_provider.lower()
    if provider_lower == "google":
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        anthropic_effort = ask_anthropic_effort()

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"]            = provider_lower
    config["deep_think_llm"]          = deep_model
    config["quick_think_llm"]         = deep_model
    config["backend_url"]             = backend_url
    config["google_thinking_level"]   = thinking_level
    config["openai_reasoning_effort"] = reasoning_effort
    config["anthropic_effort"]        = anthropic_effort

    # ── Build prompt and call LLM ──────────────────────────────────────────
    prompt_text = _build_improve_prompt(selected)

    console.print()
    console.print(Rule("[bold cyan]Generating System Improvement Report[/bold cyan]"))
    console.print()

    output: str | None = None
    with console.status("[bold yellow]Analysing reflections and generating recommendations…[/bold yellow]"):
        try:
            ta  = TradingAgentsGraph(debug=False, config=config)
            llm = ta.deep_thinking_llm
            messages = [
                SystemMessage(content=(
                    "You are a systematic trading pipeline improvement expert. "
                    "Your task is to analyse a set of trade post-mortems and produce "
                    "specific, actionable recommendations to improve the analysis pipeline, "
                    "prompts, data inputs, and scoring weights. "
                    "Be concrete: cite specific trades, propose exact prompt wording where helpful, "
                    "and give numeric weight recommendations with justification."
                )),
                HumanMessage(content=prompt_text),
            ]
            response = llm.invoke(messages)
            output   = response.content if hasattr(response, "content") else str(response)
        except Exception as exc:
            console.print(f"[red]LLM error: {exc}[/red]")
            return

    # ── Save and display ───────────────────────────────────────────────────
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = reports_dir / f"improvement_{timestamp}.md"
    reports_dir.mkdir(parents=True, exist_ok=True)
    save_path.write_text(output, encoding="utf-8")

    console.print(Panel(
        Markdown(output),
        title="[bold yellow]System Improvement Report[/bold yellow]",
        border_style="yellow", padding=(1, 2),
    ))
    console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
    console.print(f"[dim]  Based on {len(selected)} reflection(s) — bring this file to Claude Code to apply the changes.[/dim]")


@app.command()
def trades():
    """Display your full trade history from trades.json."""
    import json as _json

    trade_log_path = Path.home() / ".tradingagents" / "trades.json"
    if not trade_log_path.exists():
        console.print("[yellow]No trades.json found. Log a trade with 'uv run tradingagents reflect'.[/yellow]")
        return

    try:
        all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
    except Exception as e:
        console.print(f"[red]Error reading trades.json: {e}[/red]")
        return

    if not all_trades:
        console.print("[yellow]No trades logged yet.[/yellow]")
        return

    console.print()
    console.print(Rule("[bold cyan]Trade History[/bold cyan]"))
    console.print()

    tbl = Table(
        box=box.ROUNDED,
        title=f"[bold]All Trades ({len(all_trades)})[/bold]",
        show_lines=True,
    )
    tbl.add_column("#", justify="right", style="dim", width=3)
    tbl.add_column("Ticker", style="cyan bold", width=8)
    tbl.add_column("Sector", width=14)
    tbl.add_column("Direction", justify="center", width=10)
    tbl.add_column("Shares", justify="right", width=8)
    tbl.add_column("Entry", justify="right", width=9)
    tbl.add_column("Exit", justify="right", width=9)
    tbl.add_column("P&L $", justify="right", width=11)
    tbl.add_column("P&L %", justify="right", width=8)
    tbl.add_column("Outcome", justify="center", width=10)
    tbl.add_column("Trade Date", width=12)

    total_pnl = 0.0
    wins = losses = 0

    for i, t in enumerate(all_trades, 1):
        pnl = t.get("pnl", 0.0)
        pnl_pct = t.get("pnl_pct", 0.0)
        outcome = t.get("outcome", "?")
        direction = t.get("direction", "?")
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        pnl_color = "green" if pnl >= 0 else "red"
        dir_color = {"BUY": "green", "SHORT": "red"}.get(direction, "white")
        out_color = {"WIN": "green", "LOSS": "red", "BREAK_EVEN": "yellow"}.get(outcome, "white")
        tbl.add_row(
            str(i),
            t.get("ticker", "?"),
            t.get("sector", "—"),
            f"[{dir_color}]{direction}[/{dir_color}]",
            f"{t.get('shares', 0):.0f}",
            f"${t.get('entry_price', 0):.2f}",
            f"${t.get('exit_price', 0):.2f}",
            f"[{pnl_color}]${pnl:+.2f}[/{pnl_color}]",
            f"[{pnl_color}]{pnl_pct:+.1f}%[/{pnl_color}]",
            f"[{out_color}]{outcome}[/{out_color}]",
            t.get("trade_date", "?"),
        )

    console.print(tbl)

    n = len(all_trades)
    win_rate = wins / n * 100 if n > 0 else 0
    avg_pnl = total_pnl / n if n > 0 else 0
    pnl_color = "green" if total_pnl >= 0 else "red"
    console.print(
        f"\n  [bold]Total:[/bold] {n} trade(s)  |  "
        f"Win rate: [cyan]{win_rate:.0f}%[/cyan] ({wins}W / {losses}L)  |  "
        f"Total P&L: [{pnl_color}]${total_pnl:+.2f}[/{pnl_color}]  |  "
        f"Avg per trade: [{pnl_color}]${avg_pnl:+.2f}[/{pnl_color}]"
    )
    console.print()


@app.command()
def calibrate():
    """Calibrate screening predictions against actual earnings outcomes."""
    from tradingagents.calibration import calibrate_screening_run, list_uncalibrated_runs

    reports_dir = Path("reports")
    if not reports_dir.exists():
        console.print("[yellow]No reports/ directory found. Run 'screen' first.[/yellow]")
        return

    all_runs = sorted(reports_dir.glob("screening_*/"), key=lambda p: p.name, reverse=True)
    if not all_runs:
        console.print("[yellow]No screening runs found in reports/.[/yellow]")
        return

    console.print()
    console.print(Rule("[bold cyan]Calibration[/bold cyan]"))
    console.print()
    console.print("[bold]Available screening runs:[/bold]\n")

    for i, d in enumerate(all_runs, 1):
        has_table = (d / "screening_table.md").exists()
        has_cal = (d / "calibration.json").exists()
        status = "[green]calibrated[/green]" if has_cal else ("[yellow]not calibrated[/yellow]" if has_table else "[dim]no table[/dim]")
        console.print(f"  [cyan]{i}.[/cyan] {d.name}  {status}")

    console.print("  [dim]0. Cancel[/dim]\n")
    console.print("[dim]Enter a number to calibrate that run, or 0 to cancel.[/dim]")

    while True:
        choice = typer.prompt("").strip()
        try:
            n = int(choice)
            if n == 0:
                return
            if 1 <= n <= len(all_runs):
                selected = all_runs[n - 1]
                break
            console.print(f"[red]Enter 0–{len(all_runs)}[/red]")
        except ValueError:
            console.print("[red]Enter a number[/red]")

    if not (selected / "screening_table.md").exists():
        console.print(f"[red]No screening_table.md in {selected.name}. Cannot calibrate.[/red]")
        return

    console.print(f"\n[bold]Calibrating:[/bold] {selected.name}")
    console.print("[dim]Fetching actual earnings data from yfinance...[/dim]\n")

    try:
        with console.status("[bold yellow]Fetching actuals and computing accuracy...[/bold yellow]"):
            result = calibrate_screening_run(selected)
    except Exception as exc:
        console.print(f"[red]Calibration error: {exc}[/red]")
        return

    ba = result.get("beat_accuracy_pct")
    sa = result.get("signal_accuracy_pct")
    n_tickers = result.get("tickers", 0)

    console.print(Panel(
        f"[bold]Run:[/bold] {selected.name}\n"
        f"[bold]Tickers:[/bold] {n_tickers}\n\n"
        f"[bold]Beat prediction accuracy:[/bold] "
        f"{'[green]' + str(ba) + '%[/green]' if ba is not None else '[dim]N/A[/dim]'}\n"
        f"[bold]Signal accuracy:[/bold] "
        f"{'[green]' + str(sa) + '%[/green]' if sa is not None else '[dim]N/A[/dim]'}",
        title="[bold green]Calibration Results[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))

    cal_tbl = Table(box=box.ROUNDED, title="[bold]Per-Ticker Results[/bold]", show_lines=True)
    cal_tbl.add_column("Ticker", style="cyan bold", width=8)
    cal_tbl.add_column("Earnings", width=12)
    cal_tbl.add_column("Beat", justify="center", width=6)
    cal_tbl.add_column("Guid.", justify="center", width=6)
    cal_tbl.add_column("Setup", justify="center", width=6)
    cal_tbl.add_column("Total", justify="center", width=7)
    cal_tbl.add_column("Conf.", justify="center", width=7)
    cal_tbl.add_column("Signal", justify="center", width=8)
    cal_tbl.add_column("Actual", justify="center", width=8)
    cal_tbl.add_column("Surprise%", justify="center", width=10)
    cal_tbl.add_column("Price Δ%", justify="center", width=10)
    cal_tbl.add_column("Beat✓", justify="center", width=7)
    cal_tbl.add_column("Signal✓", justify="center", width=8)

    def _sc(n: int) -> str:
        color = "green" if n > 0 else ("red" if n < 0 else "dim")
        return f"[{color}]{n:+d}[/{color}]"

    for row in result.get("rows", []):
        b_sym = "[green]✓[/green]" if row["beat_prediction_correct"] else ("[red]✗[/red]" if row["beat_prediction_correct"] is False else "[dim]?[/dim]")
        s_sym = "[green]✓[/green]" if row["signal_correct"] else ("[red]✗[/red]" if row["signal_correct"] is False else "[dim]N/A[/dim]")
        surp = f"{row['surprise_pct']:+.1f}%" if row["surprise_pct"] is not None else "?"
        pc = f"{row['price_change_pct']:+.1f}%" if row["price_change_pct"] is not None else "?"
        act = "Beat" if row["actual_beat"] else ("Miss" if row["actual_beat"] is False else "?")
        cal_tbl.add_row(
            row["ticker"], row["earnings_date"],
            _sc(row["beat_score"]), _sc(row["guidance_score"]),
            _sc(row["setup_score"]), _sc(row["total_score"]),
            row["confidence"], row["signal"],
            act, surp, pc, b_sym, s_sym,
        )

    console.print(cal_tbl)
    console.print(f"\n[green]✓ Saved:[/green] {selected / 'calibration.json'}")
    console.print(f"[green]✓ Saved:[/green] {selected / 'calibration.md'}")
    console.print(f"[green]✓ Updated:[/green] {Path('reports') / 'calibration_master.json'}")
    console.print(f"[green]✓ Updated:[/green] {Path('reports') / 'calibration_master.md'}\n")
    _auto_build_web()


@app.command()
def correlation():
    """Correlate beat / guidance / setup scores against actual trade outcomes.

    Loads all calibration data, computes Pearson r for each score bucket vs
    signal-direction accuracy and directional price move, shows per-score-value
    accuracy tables, and suggests updated allocation weights proportional to
    each bucket's predictive power.
    """
    import json as _json
    import pandas as _pd
    from tradingagents.calibration import load_all_calibrations
    from tradingagents.allocation.weights import load_weights, save_weights

    reports_dir = Path("reports")

    console.print()
    console.print(Rule("[bold cyan]Score Correlation Analysis[/bold cyan]"))
    console.print()

    # ── Load calibration rows ─────────────────────────────────────────────────
    calibrations = load_all_calibrations(reports_dir) if reports_dir.exists() else []
    all_rows = [row for cal in calibrations for row in cal.get("rows", [])]

    if not all_rows:
        console.print(
            "[yellow]No calibration data found. "
            "Run 'tradingagents calibrate' after earnings are announced.[/yellow]"
        )
        return

    df = _pd.DataFrame(all_rows)

    for col in ["beat_score", "guidance_score", "setup_score", "total_score", "price_change_pct"]:
        if col in df.columns:
            df[col] = _pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = float("nan")

    df["signal_bin"] = df["signal_correct"].map({True: 1.0, False: 0.0})

    def _dir_change(row):
        pct = row["price_change_pct"]
        if _pd.isna(pct):
            return float("nan")
        sig = row.get("signal", "")
        return pct if sig == "BUY" else (-pct if sig == "SHORT" else float("nan"))

    df["dir_change"] = df.apply(_dir_change, axis=1)

    # ── Summary ───────────────────────────────────────────────────────────────
    n_total     = len(df)
    n_signal    = int(df["signal_bin"].notna().sum())
    n_price     = int(df["price_change_pct"].notna().sum())
    n_screening = len(calibrations)

    console.print(Panel(
        f"[bold]Screening runs:[/bold] {n_screening}  |  "
        f"[bold]Tickers screened:[/bold] {n_total}\n"
        f"[bold]With signal outcome:[/bold] {n_signal}  |  "
        f"[bold]With price data:[/bold] {n_price}",
        title="[bold cyan]Dataset[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    if n_signal < 3:
        console.print(
            "[yellow]Not enough resolved signals for correlation analysis (need ≥ 3).[/yellow]\n"
            "[dim]Run 'tradingagents calibrate' on more past screening runs.[/dim]"
        )
        return

    # ── Helper: Pearson r ─────────────────────────────────────────────────────
    def _pearson_r(s_x: "_pd.Series", s_y: "_pd.Series"):
        valid = s_x.notna() & s_y.notna()
        n = int(valid.sum())
        if n < 3:
            return None, n
        import numpy as _np
        x, y = s_x[valid].values.astype(float), s_y[valid].values.astype(float)
        mx, my = x.mean(), y.mean()
        xd, yd = x - mx, y - my
        denom = float((xd**2).sum()**0.5 * (yd**2).sum()**0.5)
        if denom == 0:
            return None, n
        return float((xd * yd).sum() / denom), n

    # ── Section 1: Per-bucket correlations ───────────────────────────────────
    score_cols = [c for c in ["beat_score", "guidance_score", "setup_score"]
                  if c in df.columns and int(df[c].notna().sum()) >= 3]

    def _r_color(r):
        if r is None:
            return "[dim]N/A[/dim]"
        ar = abs(r)
        c = "green" if ar >= 0.4 else ("yellow" if ar >= 0.2 else "red")
        return f"[{c}]{r:+.3f}[/{c}]"

    def _strength(r):
        if r is None:
            return "[dim]—[/dim]"
        ar = abs(r)
        if ar >= 0.5:
            return "[green]Strong[/green]"
        if ar >= 0.3:
            return "[yellow]Moderate[/yellow]"
        if ar >= 0.1:
            return "[dim]Weak[/dim]"
        return "[red]Negligible[/red]"

    corr_tbl = Table(
        box=box.ROUNDED,
        title="[bold]Score Correlations with Trade Outcomes[/bold]",
        show_lines=True,
    )
    corr_tbl.add_column("Bucket",       style="cyan bold", width=14)
    corr_tbl.add_column("r  signal ✓",  justify="center",  width=14)
    corr_tbl.add_column("r  dir.move",  justify="center",  width=14)
    corr_tbl.add_column("N (signal)",   justify="right",   width=11)
    corr_tbl.add_column("Strength",     width=14)

    abs_corrs: dict = {}
    for col in score_cols:
        r_sig, n_sig = _pearson_r(df[col], df["signal_bin"])
        r_prc, _     = _pearson_r(df[col], df["dir_change"])
        if r_sig is not None:
            abs_corrs[col] = abs(r_sig)
        corr_tbl.add_row(
            col.replace("_score", "").capitalize(),
            _r_color(r_sig),
            _r_color(r_prc),
            str(n_sig),
            _strength(r_sig),
        )

    console.print(corr_tbl)
    console.print(
        "[dim]r = Pearson r.  signal ✓ = 1 when predicted direction matched price move.  "
        "dir.move = price change in signal direction (BUY→+, SHORT→−).[/dim]\n"
    )

    # ── Section 2: Per-score-value accuracy breakdown ─────────────────────────
    resolved = df[df["signal_bin"].notna()].copy()
    for col in score_cols:
        bucket_name = col.replace("_score", "").capitalize()
        grp = (
            resolved.groupby(col)["signal_bin"]
            .agg(["sum", "count"])
            .rename(columns={"sum": "correct", "count": "total"})
            .reset_index()
        )
        if grp.empty:
            continue

        bkt_tbl = Table(
            box=box.ROUNDED,
            title=f"[bold]{bucket_name} Score — Signal Accuracy by Value[/bold]",
            show_lines=True,
        )
        bkt_tbl.add_column("Score",          justify="center", style="cyan", width=8)
        bkt_tbl.add_column("Signals",         justify="right",               width=9)
        bkt_tbl.add_column("Correct",         justify="right",               width=9)
        bkt_tbl.add_column("Accuracy",        justify="center",              width=10)
        bkt_tbl.add_column("Avg Dir Move %",  justify="right",               width=15)

        for _, row in grp.sort_values(col).iterrows():
            score_val = int(row[col])
            correct   = int(row["correct"])
            total     = int(row["total"])
            acc       = row["correct"] / row["total"] * 100 if row["total"] > 0 else 0
            sub_price = resolved[resolved[col] == score_val]["dir_change"].dropna()
            avg_move  = float(sub_price.mean()) if len(sub_price) > 0 else float("nan")

            acc_color = "green" if acc >= 60 else ("yellow" if acc >= 40 else "red")
            import math as _math
            move_str  = (
                f"[{'green' if avg_move > 0 else 'red'}]{avg_move:+.1f}%[/{'green' if avg_move > 0 else 'red'}]"
                if not _math.isnan(avg_move) else "[dim]N/A[/dim]"
            )
            bkt_tbl.add_row(
                f"{score_val:+d}",
                str(total),
                str(correct),
                f"[{acc_color}]{acc:.0f}%[/{acc_color}]",
                move_str,
            )
        console.print(bkt_tbl)

    # ── Section 3: Score intercorrelations ────────────────────────────────────
    if len(score_cols) >= 2:
        ic_tbl = Table(
            box=box.ROUNDED,
            title="[bold]Score Intercorrelations[/bold]",
            show_lines=True,
        )
        ic_tbl.add_column("", style="cyan bold", width=14)
        for col in score_cols:
            ic_tbl.add_column(col.replace("_score", "").capitalize(), justify="center", width=12)

        for col_a in score_cols:
            row_vals = [col_a.replace("_score", "").capitalize()]
            for col_b in score_cols:
                if col_a == col_b:
                    row_vals.append("[dim]—[/dim]")
                else:
                    r, _ = _pearson_r(df[col_a], df[col_b])
                    row_vals.append(_r_color(r))
            ic_tbl.add_row(*row_vals)

        console.print(ic_tbl)
        console.print(
            "[dim]High intercorrelation (|r| > 0.7) = two buckets carry similar information. "
            "Consider down-weighting the less predictive one.[/dim]\n"
        )

    # ── Section 4: Suggested weights ─────────────────────────────────────────
    current_weights = load_weights()
    bucket_key_map  = {"beat_score": "beat", "guidance_score": "guidance", "setup_score": "setup"}

    if abs_corrs:
        total_r  = sum(abs_corrs.values()) or 1.0
        n_b      = len(abs_corrs)
        suggested = {
            bucket_key_map[k]: round(v / total_r * n_b, 2)
            for k, v in abs_corrs.items()
        }
        for k in ("beat", "guidance", "setup"):
            if k not in suggested:
                suggested[k] = current_weights.get(k, 1.0)

        wt_tbl = Table(
            box=box.ROUNDED,
            title="[bold]Suggested Allocation Weights[/bold]",
            show_lines=True,
        )
        wt_tbl.add_column("Bucket",    style="cyan bold", width=14)
        wt_tbl.add_column("|r|",       justify="center",  width=8)
        wt_tbl.add_column("Current",   justify="center",  width=10)
        wt_tbl.add_column("Suggested", justify="center",  width=12)
        wt_tbl.add_column("Change",    justify="center",  width=12)

        apply_args = []
        for col in score_cols:
            key   = bucket_key_map[col]
            ar    = abs_corrs.get(col, 0.0)
            cur   = current_weights.get(key, 1.0)
            sug   = suggested[key]
            delta = sug - cur
            if delta > 0.05:
                delta_str = f"[green]+{delta:.2f}[/green]"
            elif delta < -0.05:
                delta_str = f"[red]{delta:.2f}[/red]"
            else:
                delta_str = "[dim]≈[/dim]"
            wt_tbl.add_row(
                key.capitalize(),
                f"{ar:.3f}",
                f"{cur:.2f}",
                f"[bold]{sug:.2f}[/bold]",
                delta_str,
            )
            apply_args.append((key, sug))

        console.print(wt_tbl)
        console.print(
            "[dim]Suggested weights are proportional to |r| with signal correctness, "
            "normalized so their average equals 1.0.[/dim]\n"
        )

        apply = typer.prompt("Apply suggested weights? [y/N]", default="N").strip().upper()
        if apply in ("Y", "YES"):
            beat_w  = suggested.get("beat",     current_weights["beat"])
            guid_w  = suggested.get("guidance", current_weights["guidance"])
            setup_w = suggested.get("setup",    current_weights["setup"])
            save_weights(beat_w, guid_w, setup_w)
            console.print(
                f"[green]✓ Weights updated:[/green] "
                f"beat=[cyan]{beat_w:.2f}[/cyan]  "
                f"guidance=[cyan]{guid_w:.2f}[/cyan]  "
                f"setup=[cyan]{setup_w:.2f}[/cyan]"
            )
        else:
            manual_cmd = "tradingagents allocation-weights " + " ".join(
                f"--{k} {v:.2f}" for k, v in apply_args
            )
            console.print(f"[dim]To apply manually: {manual_cmd}[/dim]")
    else:
        console.print("[dim]Not enough data to suggest weights.[/dim]")

    console.print()


@app.command()
def stats():
    """Display accuracy statistics from trade history and calibration results."""
    import json as _json
    from tradingagents.calibration import load_all_calibrations

    console.print()
    console.print(Rule("[bold cyan]TradingAgents Statistics[/bold cyan]"))
    console.print()

    # --- Trade History Stats ---
    trade_log_path = Path.home() / ".tradingagents" / "trades.json"
    all_trades = []
    if trade_log_path.exists():
        try:
            all_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if all_trades:
        n = len(all_trades)
        wins = sum(1 for t in all_trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in all_trades if t.get("pnl", 0) < 0)
        total_pnl = sum(t.get("pnl", 0) for t in all_trades)
        win_rate = wins / n * 100 if n else 0

        beat_data = [t for t in all_trades if t.get("beat_prediction_correct") is not None]
        beat_acc = sum(1 for t in beat_data if t.get("beat_prediction_correct")) / len(beat_data) * 100 if beat_data else None

        guid_data = [t for t in all_trades if t.get("guidance_prediction_correct") is not None]
        guid_acc = sum(1 for t in guid_data if t.get("guidance_prediction_correct")) / len(guid_data) * 100 if guid_data else None

        pnl_color = "green" if total_pnl >= 0 else "red"

        console.print(Panel(
            f"[bold]Trades:[/bold] {n}  |  [bold]Wins:[/bold] {wins}  |  [bold]Losses:[/bold] {losses}\n"
            f"[bold]Win rate:[/bold] [cyan]{win_rate:.0f}%[/cyan]  |  "
            f"[bold]Total P&L:[/bold] [{pnl_color}]${total_pnl:+.2f}[/{pnl_color}]\n"
            f"[bold]Beat prediction accuracy:[/bold] "
            f"{'[green]' + f'{beat_acc:.0f}%[/green]  (' + str(len(beat_data)) + ' trades)' if beat_acc is not None else '[dim]N/A (no reflection data)[/dim]'}\n"
            f"[bold]Guidance prediction accuracy:[/bold] "
            f"{'[green]' + f'{guid_acc:.0f}%[/green]  (' + str(len(guid_data)) + ' trades)' if guid_acc is not None else '[dim]N/A (no reflection data)[/dim]'}",
            title="[bold cyan]Trade History[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        ))

        # By direction
        for direction in ("BUY", "SHORT"):
            dir_trades = [t for t in all_trades if t.get("direction") == direction]
            if dir_trades:
                dw = sum(1 for t in dir_trades if t.get("pnl", 0) > 0)
                dr = dw / len(dir_trades) * 100
                dir_color = "green" if direction == "BUY" else "red"
                console.print(f"  [{dir_color}]{direction}[/{dir_color}]: {len(dir_trades)} trade(s), win rate [cyan]{dr:.0f}%[/cyan]")
        console.print()
    else:
        console.print("[dim]No trades logged yet. Run 'uv run tradingagents reflect' after closing a trade.[/dim]\n")

    # --- Calibration Stats ---
    reports_dir = Path("reports")
    calibrations = load_all_calibrations(reports_dir) if reports_dir.exists() else []

    if calibrations:
        all_rows = [row for cal in calibrations for row in cal.get("rows", [])]

        with_beat = [r for r in all_rows if r["beat_prediction_correct"] is not None]
        with_signal = [r for r in all_rows if r["signal_correct"] is not None]

        beat_acc_cal = sum(1 for r in with_beat if r["beat_prediction_correct"]) / len(with_beat) * 100 if with_beat else None
        sig_acc_cal = sum(1 for r in with_signal if r["signal_correct"]) / len(with_signal) * 100 if with_signal else None

        # By confidence
        conf_stats = {}
        for r in with_signal:
            c = r.get("confidence", "?")
            if c not in conf_stats:
                conf_stats[c] = {"correct": 0, "total": 0}
            conf_stats[c]["total"] += 1
            if r["signal_correct"]:
                conf_stats[c]["correct"] += 1

        # By score bucket
        bucket_stats: dict[str, dict] = {}
        for r in with_signal:
            ts = r.get("total_score", 0)
            if ts >= 8:
                bucket = "≥+8 (strong)"
            elif ts >= 4:
                bucket = "+4 to +7"
            elif ts >= 0:
                bucket = "0 to +3"
            elif ts >= -4:
                bucket = "-1 to -4"
            else:
                bucket = "≤-5 (bearish)"
            if bucket not in bucket_stats:
                bucket_stats[bucket] = {"correct": 0, "total": 0}
            bucket_stats[bucket]["total"] += 1
            if r["signal_correct"]:
                bucket_stats[bucket]["correct"] += 1

        cal_summary = (
            f"[bold]Runs calibrated:[/bold] {len(calibrations)}  |  "
            f"[bold]Tickers:[/bold] {len(all_rows)}\n"
            f"[bold]Beat prediction accuracy:[/bold] "
            f"{'[green]' + f'{beat_acc_cal:.0f}%[/green]  (' + str(len(with_beat)) + ' tickers)' if beat_acc_cal is not None else '[dim]N/A[/dim]'}\n"
            f"[bold]Signal accuracy:[/bold] "
            f"{'[green]' + f'{sig_acc_cal:.0f}%[/green]  (' + str(len(with_signal)) + ' tickers)' if sig_acc_cal is not None else '[dim]N/A[/dim]'}"
        )
        console.print(Panel(cal_summary, title="[bold magenta]Calibration (Screening Accuracy)[/bold magenta]", border_style="magenta", padding=(1, 2)))

        if conf_stats:
            conf_tbl = Table(box=box.ROUNDED, title="[bold]Signal Accuracy by Confidence[/bold]", show_lines=True)
            conf_tbl.add_column("Confidence", style="cyan", width=12)
            conf_tbl.add_column("Signals", justify="right", width=9)
            conf_tbl.add_column("Correct", justify="right", width=9)
            conf_tbl.add_column("Accuracy", justify="center", width=10)
            for conf, s in sorted(conf_stats.items()):
                acc = s["correct"] / s["total"] * 100 if s["total"] else 0
                acc_color = "green" if acc >= 60 else ("yellow" if acc >= 40 else "red")
                conf_tbl.add_row(conf, str(s["total"]), str(s["correct"]), f"[{acc_color}]{acc:.0f}%[/{acc_color}]")
            console.print(conf_tbl)

        if bucket_stats:
            bucket_tbl = Table(box=box.ROUNDED, title="[bold]Signal Accuracy by Total Score Bucket[/bold]", show_lines=True)
            bucket_tbl.add_column("Score Bucket", style="cyan", width=18)
            bucket_tbl.add_column("Signals", justify="right", width=9)
            bucket_tbl.add_column("Correct", justify="right", width=9)
            bucket_tbl.add_column("Accuracy", justify="center", width=10)
            bucket_order = ["≥+8 (strong)", "+4 to +7", "0 to +3", "-1 to -4", "≤-5 (bearish)"]
            for bucket in bucket_order:
                if bucket in bucket_stats:
                    s = bucket_stats[bucket]
                    acc = s["correct"] / s["total"] * 100 if s["total"] else 0
                    acc_color = "green" if acc >= 60 else ("yellow" if acc >= 40 else "red")
                    bucket_tbl.add_row(bucket, str(s["total"]), str(s["correct"]), f"[{acc_color}]{acc:.0f}%[/{acc_color}]")
            console.print(bucket_tbl)
    else:
        console.print("[dim]No calibration data yet. Run 'uv run tradingagents calibrate' after earnings are announced.[/dim]\n")

    # ── Capital & Benchmark Comparison ────────────────────────────────────────
    if not all_trades:
        return

    import yfinance as _yf
    import datetime as _dt
    import math as _math

    console.print(Rule("[bold cyan]Capital & Benchmark Comparison[/bold cyan]"))
    console.print()

    # Consolidate fills into single trades by (ticker, exit_date)
    _groups: dict = {}
    for t in all_trades:
        key = (t.get("ticker", ""), t.get("exit_date", ""))
        sh  = t.get("shares", 0) or 0
        ep  = t.get("entry_price", 0) or 0
        if key not in _groups:
            _groups[key] = {"pnl": 0.0, "shares": 0.0, "_entry_wtd": 0.0, "_sh_wtd": 0.0, "exit_date": key[1]}
        g = _groups[key]
        g["pnl"]        += t.get("pnl", 0) or 0
        g["shares"]     += sh
        g["_entry_wtd"] += ep * sh
        g["_sh_wtd"]    += sh

    consolidated = []
    for g in _groups.values():
        avg_ep  = g["_entry_wtd"] / g["_sh_wtd"] if g["_sh_wtd"] else 0
        cost    = avg_ep * g["_sh_wtd"]
        consolidated.append({"exit_date": g["exit_date"], "cost_basis": cost, "pnl": g["pnl"]})

    if not consolidated:
        return

    # Daily capital summary (proxy: capital deployed on each exit date)
    _daily: dict = {}
    for c in consolidated:
        d = c["exit_date"]
        _daily[d] = _daily.get(d, 0.0) + c["cost_basis"]

    sorted_dates   = sorted(_daily.keys())
    first_date_str = sorted_dates[0]
    last_date_str  = sorted_dates[-1]
    today_str      = _dt.date.today().isoformat()

    total_cost     = sum(c["cost_basis"] for c in consolidated)
    total_pnl_val  = sum(c["pnl"]        for c in consolidated)
    actual_ret_pct = total_pnl_val / total_cost * 100 if total_cost else 0
    avg_daily_cap  = sum(_daily.values()) / len(_daily)
    n_active_days  = len(_daily)

    # Calendar days from first trade to today
    try:
        d0 = _dt.date.fromisoformat(first_date_str)
        d1 = _dt.date.today()
        cal_days = (d1 - d0).days or 1
    except Exception:
        cal_days = 1

    # Daily capital table
    daily_tbl = Table(box=box.ROUNDED, title="[bold]Capital Deployed by Day[/bold]", show_lines=True)
    daily_tbl.add_column("Exit Date",     style="cyan",  width=13)
    daily_tbl.add_column("Capital",       justify="right", width=14)
    daily_tbl.add_column("Day P&L",       justify="right", width=12)
    daily_tbl.add_column("Day Return",    justify="right", width=11)

    daily_pnl_by_date: dict = {}
    for c in consolidated:
        d = c["exit_date"]
        daily_pnl_by_date[d] = daily_pnl_by_date.get(d, 0.0) + c["pnl"]

    for d in sorted_dates:
        cap     = _daily[d]
        day_pnl = daily_pnl_by_date.get(d, 0.0)
        day_ret = day_pnl / cap * 100 if cap else 0
        pnl_c   = "green" if day_pnl >= 0 else "red"
        daily_tbl.add_row(
            d,
            f"${cap:,.0f}",
            f"[{pnl_c}]{'+' if day_pnl >= 0 else ''}{day_pnl:,.0f}[/{pnl_c}]",
            f"[{pnl_c}]{day_ret:+.2f}%[/{pnl_c}]",
        )

    avg_c = "green" if avg_daily_cap >= 0 else "red"
    pnl_c = "green" if total_pnl_val >= 0 else "red"
    daily_tbl.add_section()
    daily_tbl.add_row(
        "[bold]Average / Total[/bold]",
        f"[bold]${avg_daily_cap:,.0f}[/bold] avg",
        f"[{pnl_c}][bold]{'+' if total_pnl_val >= 0 else ''}{total_pnl_val:,.0f}[/bold][/{pnl_c}]",
        f"[{pnl_c}][bold]{actual_ret_pct:+.2f}%[/bold][/{pnl_c}]",
    )
    console.print(daily_tbl)
    console.print(
        f"[dim]Total cost basis across all positions: ${total_cost:,.0f}  |  "
        f"{n_active_days} active trading days[/dim]\n"
    )

    # Benchmark: invest avg_daily_capital on first_date, hold to today
    console.print(f"[dim]Fetching QQQ and SPY prices ({first_date_str} → {today_str})…[/dim]")
    bench_rows = []
    for sym in ("QQQ", "SPY"):
        try:
            df = _yf.download(sym, start=first_date_str, end=today_str, progress=False, auto_adjust=True)
            if df.empty:
                continue
            close = df["Close"].squeeze()
            p0    = float(close.iloc[0])
            p1    = float(close.iloc[-1])
            ret   = (p1 - p0) / p0
            bench_pnl = avg_daily_cap * ret
            bench_rows.append({
                "sym":  sym,
                "p0":   p0,
                "p1":   p1,
                "ret":  ret * 100,
                "pnl":  bench_pnl,
                "diff": total_pnl_val - bench_pnl,
            })
        except Exception:
            pass

    if bench_rows:
        bm_tbl = Table(
            box=box.ROUNDED,
            title=(
                f"[bold]Benchmark: avg daily capital ${avg_daily_cap:,.0f} "
                f"invested on {first_date_str} vs today[/bold]"
            ),
            show_lines=True,
        )
        bm_tbl.add_column("",            style="cyan bold", width=10)
        bm_tbl.add_column("Entry price", justify="right",   width=13)
        bm_tbl.add_column("Today",       justify="right",   width=11)
        bm_tbl.add_column("Return %",    justify="center",  width=11)
        bm_tbl.add_column("P&L $",       justify="right",   width=13)
        bm_tbl.add_column("vs Your P&L", justify="right",   width=16)
        bm_tbl.add_column("Winner",      justify="center",  width=12)

        ret_c = "green" if actual_ret_pct >= 0 else "red"
        bm_tbl.add_row(
            "[bold]You[/bold]",
            "[dim]—[/dim]", "[dim]—[/dim]",
            f"[{ret_c}]{actual_ret_pct:+.2f}%[/{ret_c}]",
            f"[{ret_c}]{'+' if total_pnl_val >= 0 else ''}{total_pnl_val:,.0f}[/{ret_c}]",
            "[dim]—[/dim]",
            "[dim]—[/dim]",
        )

        for br in bench_rows:
            rc      = "green" if br["ret"] >= 0 else "red"
            dc      = "green" if br["diff"] >= 0 else "red"
            winner  = "[green]You ✓[/green]" if br["diff"] > 0 else f"[yellow]{br['sym']}[/yellow]"
            bm_tbl.add_row(
                br["sym"],
                f"${br['p0']:,.2f}",
                f"${br['p1']:,.2f}",
                f"[{rc}]{br['ret']:+.2f}%[/{rc}]",
                f"[{rc}]{'+' if br['pnl'] >= 0 else ''}{br['pnl']:,.0f}[/{rc}]",
                f"[{dc}]{'+' if br['diff'] >= 0 else ''}{br['diff']:,.0f}[/{dc}]",
                winner,
            )

        console.print(bm_tbl)
        console.print(
            f"[dim]Benchmark: ${avg_daily_cap:,.0f} avg daily capital invested on "
            f"{first_date_str} (first exit date) and held until today ({today_str}).\n"
            "Entry dates unavailable for IBKR-imported trades — exit dates used as period proxy.[/dim]\n"
        )
    else:
        console.print("[dim]Could not fetch benchmark prices (check network connection).[/dim]\n")


@app.command()
def allocate(
    budget: int = typer.Option(100_000, "--budget", help="Capital budget for allocation ($)"),
    dir: Optional[str] = typer.Option(None, "--dir", "-d", help="Path to screening directory (skips interactive picker)"),
):
    """Rebuild the screening table and re-run the Allocation Manager.

    Reads scores directly from each ticker's earnings_brief.md, regenerates
    screening_table.md, then runs allocation. Useful when combining tickers
    from multiple sessions or correcting a bad table.
    """
    import json as _json
    import re as _re

    reports_dir = Path("reports")

    # --- Pick screening directory ---
    if dir:
        screening_dir = Path(dir)
    else:
        candidates = sorted(
            [d for d in reports_dir.glob("screening_*/") if d.is_dir()],
            reverse=True,
        )
        if not candidates:
            console.print("[red]No screening_* directories found in reports/.[/red]")
            raise typer.Exit(1)

        console.print("\n[bold]Select a screening directory:[/bold]\n")
        for i, d in enumerate(candidates, 1):
            ticker_dirs = [t for t in d.iterdir() if t.is_dir() and (t / "earnings_brief.md").exists()]
            alloc_tag = "[dim](allocation exists)[/dim]" if (d / "allocation.md").exists() else ""
            console.print(f"  [cyan]{i}.[/cyan] {d.name}  [dim]{len(ticker_dirs)} tickers[/dim] {alloc_tag}")

        console.print()
        choice = questionary.text("Enter number:").ask()
        if not choice:
            raise typer.Exit(0)
        try:
            n = int(choice.strip())
            screening_dir = candidates[n - 1]
        except (ValueError, IndexError):
            console.print("[red]Invalid selection.[/red]")
            raise typer.Exit(1)

    if not screening_dir.exists():
        console.print(f"[red]Directory not found: {screening_dir}[/red]")
        raise typer.Exit(1)

    # --- Select LLM provider for allocation ---
    selected_provider, backend_url = select_llm_provider()
    deep_model = select_deep_thinking_agent(selected_provider)
    provider_lower = selected_provider.lower()
    thinking_level = reasoning_effort = anthropic_effort = None
    if provider_lower == "google":
        thinking_level = ask_gemini_thinking_config()
    elif provider_lower == "openai":
        reasoning_effort = ask_openai_reasoning_effort()
    elif provider_lower == "anthropic":
        anthropic_effort = ask_anthropic_effort()

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = provider_lower
    config["deep_think_llm"] = deep_model
    config["quick_think_llm"] = deep_model
    config["backend_url"] = backend_url
    config["google_thinking_level"] = thinking_level
    config["openai_reasoning_effort"] = reasoning_effort
    config["anthropic_effort"] = anthropic_effort

    # --- Rebuild results from earnings_brief.md files ---
    def _parse_brief(ticker_dir: Path) -> dict | None:
        brief_path = ticker_dir / "earnings_brief.md"
        if not brief_path.exists():
            return None
        text = brief_path.read_text(encoding="utf-8")
        m = _re.search(r"```json\s*(\{.*?\})\s*```", text, _re.DOTALL)
        if not m:
            return None
        try:
            scores = _json.loads(m.group(1))
        except _json.JSONDecodeError:
            return None
        return {
            "ticker": ticker_dir.name,
            "earnings_date": scores.get("earnings_date", "unknown"),
            "beat_score": int(scores.get("beat_score", 0)),
            "guidance_score": int(scores.get("guidance_score", 0)),
            "setup_score": int(scores.get("setup_score", 0)),
            "total_score": int(scores.get("total_score", 0)),
            "signal": scores.get("signal", "SKIP"),
            "confidence": scores.get("confidence", "?"),
            "one_liner": scores.get("one_liner", ""),
        }

    ticker_dirs = sorted(
        [d for d in screening_dir.iterdir() if d.is_dir() and (d / "earnings_brief.md").exists()]
    )
    if not ticker_dirs:
        console.print(f"[red]No ticker folders with earnings_brief.md found in {screening_dir}[/red]")
        raise typer.Exit(1)

    results = []
    with console.status("[dim]Reading ticker briefs and fetching sectors...[/dim]"):
        for td in ticker_dirs:
            r = _parse_brief(td)
            if r is None:
                console.print(f"[yellow]  Skipping {td.name} — could not parse earnings_brief.md[/yellow]")
                continue
            r["sector"] = _fetch_sector(r["ticker"])
            results.append(r)

    if not results:
        console.print("[red]No valid ticker results found.[/red]")
        raise typer.Exit(1)

    sorted_results = sorted(results, key=lambda r: r.get("total_score", 0), reverse=True)

    # Extract trade_date from folder name (screening_YYYY-MM-DD_...)
    name_parts = screening_dir.name.split("_")
    try:
        trade_date = name_parts[1]
    except IndexError:
        from datetime import date as _date
        trade_date = str(_date.today())

    # --- Regenerate screening_table.md ---
    depth_label = "Rescreened"
    table_lines = [
        f"# Earnings Screener — {depth_label} — {trade_date}\n\n",
        "| # | Ticker | Sector | Earnings | Beat | Guidance | Setup | Total | Signal | Confidence | One-liner |\n",
        "|---|--------|--------|----------|------|----------|-------|-------|--------|------------|-----------|\n",
    ]
    for i, r in enumerate(sorted_results, 1):
        table_lines.append(
            f"| {i} | {r['ticker']} | {r.get('sector','Unknown')} | {r.get('earnings_date','?')} "
            f"| {r.get('beat_score',0):+d} | {r.get('guidance_score',0):+d} "
            f"| {r.get('setup_score',0):+d} | {r.get('total_score',0):+d} "
            f"| {r.get('signal','?')} | {r.get('confidence','?')} "
            f"| {r.get('one_liner','')} |\n"
        )
    (screening_dir / "screening_table.md").write_text("".join(table_lines), encoding="utf-8")
    console.print(f"[green]✓ screening_table.md rebuilt[/green] ({len(sorted_results)} tickers)\n")

    # --- Print table ---
    def sc(n: int) -> str:
        style = "green" if n > 0 else ("red" if n < 0 else "dim")
        return f"[{style}]{n:+d}[/{style}]"

    tbl = Table(box=box.ROUNDED, title=f"[bold]Earnings Screener — {trade_date}[/bold]", show_lines=True)
    tbl.add_column("#", justify="right", style="dim", width=3)
    tbl.add_column("Ticker", style="cyan bold", width=8)
    tbl.add_column("Sector", width=16)
    tbl.add_column("Earnings", width=12)
    tbl.add_column("Beat", justify="center", width=6)
    tbl.add_column("Guidance", justify="center", width=9)
    tbl.add_column("Setup", justify="center", width=7)
    tbl.add_column("Total", justify="center", width=7)
    tbl.add_column("Signal", justify="center", width=8)
    tbl.add_column("Conf.", justify="center", width=7)
    tbl.add_column("One-liner", no_wrap=False, min_width=30)
    for i, r in enumerate(sorted_results, 1):
        total = r.get("total_score", 0)
        signal = r.get("signal", "?")
        signal_color = {"BUY": "green", "SHORT": "red", "SKIP": "yellow"}.get(signal, "white")
        total_color = "green" if total > 0 else ("red" if total < 0 else "dim")
        tbl.add_row(
            str(i), r["ticker"], r.get("sector", "Unknown"), r.get("earnings_date", "?"),
            sc(r.get("beat_score", 0)), sc(r.get("guidance_score", 0)), sc(r.get("setup_score", 0)),
            f"[{total_color}]{total:+d}[/{total_color}]",
            f"[{signal_color}]{signal}[/{signal_color}]",
            r.get("confidence", "?"), r.get("one_liner", ""),
        )
    console.print(tbl)

    # --- Run Allocation Manager (AI Council) ---
    console.print()
    console.print(Rule("[bold magenta]Allocation Manager — AI Council[/bold magenta]"))
    allocation_report = None
    try:
        ta_alloc = TradingAgentsGraph(debug=False, config=config)
        alloc_layer = AllocationLayer(llm=ta_alloc.deep_thinking_llm, budget=budget)
        allocation_report = alloc_layer.allocate(
            results=sorted_results,
            trade_date=trade_date,
            screening_dir=screening_dir,
            save=True,
            progress_cb=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
        )
    except Exception as exc:
        console.print(f"[red]Allocation Manager error: {exc}[/red]")
        raise typer.Exit(1)

    if allocation_report:
        console.print(
            Panel(
                Markdown(allocation_report),
                title=f"[bold magenta]Portfolio Allocation — ${budget:,}[/bold magenta]",
                border_style="magenta",
                padding=(1, 2),
            )
        )

        alloc_data = parse_allocation(allocation_report)
        allocations = alloc_data.get("allocations", [])
        if allocations:
            alloc_table = Table(box=box.ROUNDED, title="[bold]Allocation Summary[/bold]", show_lines=True)
            alloc_table.add_column("Ticker", style="cyan bold", width=8)
            alloc_table.add_column("Direction", justify="center", width=10)
            alloc_table.add_column("Amount", justify="right", width=12)
            alloc_table.add_column("% Budget", justify="center", width=9)
            alloc_table.add_column("Conviction", justify="center", width=10)
            alloc_table.add_column("Rationale", no_wrap=False, min_width=30)
            for a in allocations:
                direction = a.get("direction", "SKIP")
                dir_color = {"BUY": "green", "SHORT": "red", "SKIP": "dim"}.get(direction, "white")
                amount = a.get("amount", 0)
                alloc_table.add_row(
                    a.get("ticker", ""),
                    f"[{dir_color}]{direction}[/{dir_color}]",
                    f"[{dir_color}]{'$'+f'{amount:,}' if amount else '—'}[/{dir_color}]",
                    f"{a.get('pct_of_budget', 0):.1f}%",
                    a.get("conviction", ""),
                    a.get("rationale", ""),
                )
            deployed = alloc_data.get("total_deployed", 0)
            cash = alloc_data.get("cash_reserved", 0)
            console.print(alloc_table)
            console.print(
                f"  Deployed: [green]${deployed:,}[/green]  "
                f"Cash: [yellow]${cash:,}[/yellow]  "
                f"Long: [green]${alloc_data.get('long_exposure', 0):,}[/green]  "
                f"Short: [red]${alloc_data.get('short_exposure', 0):,}[/red]"
            )

        console.print(f"\n[green]✓ Saved to:[/green] {(screening_dir / 'allocation.md').resolve()}")


def _get_analyzed_tickers(reports_dir: Path) -> dict[str, str]:
    """Return mapping of ticker → earliest analysis date (YYYY-MM-DD string).

    Scans both screening run subdirs and individual analysis dirs.
    The earliest date is kept so a trade is valid from first analysis onward.
    """
    ticker_dates: dict[str, str] = {}
    if not reports_dir.exists():
        return ticker_dates

    def _keep_earliest(ticker: str, date_str: str) -> None:
        existing = ticker_dates.get(ticker)
        if existing is None or date_str < existing:
            ticker_dates[ticker] = date_str

    # Screening runs: screening_YYYY-MM-DD_YYYYMMDD_HHMMSS/TICKER/
    from datetime import datetime as _dt
    for d in reports_dir.glob("screening_*/"):
        parts = d.name.split("_")
        try:
            date_str = parts[1]  # "2026-04-26"
            _dt.strptime(date_str, "%Y-%m-%d")
        except (IndexError, ValueError):
            continue
        for t in d.iterdir():
            if t.is_dir() and not t.suffix:
                _keep_earliest(t.name, date_str)

    # Individual analysis runs: TICKER_YYYYMMDD_HHMMSS/
    for d in reports_dir.iterdir():
        if (d.is_dir() and "_" in d.name
                and not d.name.startswith("screening_")
                and not d.name.startswith("reflections")):
            parts = d.name.split("_")
            try:
                date_str = _dt.strptime(parts[1], "%Y%m%d").strftime("%Y-%m-%d")
            except (IndexError, ValueError):
                continue
            _keep_earliest(parts[0], date_str)

    return ticker_dates


@app.command("import-ibkr")
def import_ibkr(
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Path to a downloaded Flex XML report (skips API call)"),
    all_trades: bool = typer.Option(False, "--all", help="Import all trades, not just TradingAgents-analyzed tickers"),
):
    """Import closed trades from IBKR.

    By default only imports tickers that have been screened or analyzed by
    TradingAgents. Use --all to import everything.

    Two modes:
      --file path/to/report.xml   Parse a manually downloaded Flex XML file.
      (no flag)                   Download automatically via the Flex API
                                  (requires IBKR_FLEX_TOKEN + IBKR_FLEX_QUERY_ID in .env).
    """
    import json as _json
    import os
    from tradingagents.ibkr import download_flex_xml, parse_closing_trades

    console.print()
    console.print(Rule("[bold cyan]IBKR Trade Import[/bold cyan]"))
    console.print()

    # --- Get XML: from file or via API ---
    xml_str = None

    if file:
        xml_path = Path(file)
        if not xml_path.exists():
            console.print(f"[red]File not found: {xml_path}[/red]")
            return
        xml_str = xml_path.read_text(encoding="utf-8")
        console.print(f"[dim]Reading: {xml_path.resolve()}[/dim]\n")
    else:
        token = os.environ.get("IBKR_FLEX_TOKEN", "").strip()
        query_id = os.environ.get("IBKR_FLEX_QUERY_ID", "").strip()

        if not token or not query_id:
            console.print(Panel(
                "[yellow]No --file provided and no API credentials found.[/yellow]\n\n"
                "[bold]Option A — manual file (recommended):[/bold]\n"
                "  1. Go to IBKR portal → Performance & Reports → Flex Queries\n"
                "  2. Click the [bold]→[/bold] (run) button next to your TradingAgents query\n"
                "  3. Download the XML report\n"
                "  4. Run: [bold]uv run tradingagents import-ibkr --file path/to/report.xml[/bold]\n\n"
                "[bold]Option B — automatic API:[/bold]\n"
                "  Add to [bold].env[/bold]:\n"
                "    IBKR_FLEX_TOKEN=your_token\n"
                "    IBKR_FLEX_QUERY_ID=1495116",
                border_style="yellow",
                padding=(1, 2),
            ))
            return

        console.print("[dim]Connecting to IBKR Flex Web Service...[/dim]")
        with console.status("[bold yellow]Downloading report (retries automatically on server busy errors — up to ~60s)...[/bold yellow]"):
            try:
                xml_str = download_flex_xml(token, query_id)
            except Exception as exc:
                console.print(f"[red]Download failed: {exc}[/red]")
                return

    # --- Parse trades ---
    try:
        ibkr_trades = parse_closing_trades(xml_str)
    except Exception as exc:
        console.print(f"[red]Parse error: {exc}[/red]")
        return

    if not ibkr_trades:
        console.print("[yellow]No closing stock trades found in the report.[/yellow]")
        return

    # Filter to analyzed tickers with trades after the analysis date, unless --all
    if not all_trades:
        analyzed = _get_analyzed_tickers(Path("reports"))
        before = len(ibkr_trades)

        def _should_import(t: dict) -> bool:
            ticker = t.get("ticker", "")
            analysis_date = analyzed.get(ticker)
            if analysis_date is None:
                return False
            exit_date = t.get("exit_date", "")
            return bool(exit_date) and exit_date >= analysis_date

        ibkr_trades = [t for t in ibkr_trades if _should_import(t)]
        filtered_out = before - len(ibkr_trades)
        if filtered_out:
            console.print(
                f"[dim]Filtered out {filtered_out} trade(s) (ticker not analyzed by TradingAgents, "
                f"or traded before analysis date). Use --all to import everything.[/dim]\n"
            )
        if not ibkr_trades:
            console.print("[yellow]No trades remain after filtering. Use --all to import everything.[/yellow]")
            return

    console.print(f"[green]Found {len(ibkr_trades)} closing trade(s) in the report.[/green]\n")

    # --- Load existing trades.json to deduplicate ---
    trade_log_path = Path.home() / ".tradingagents" / "trades.json"
    trade_log_path.parent.mkdir(parents=True, exist_ok=True)
    existing_trades = []
    if trade_log_path.exists():
        try:
            existing_trades = _json.loads(trade_log_path.read_text(encoding="utf-8"))
        except Exception:
            existing_trades = []

    existing_ids = {
        t.get("ibkr_trade_id") for t in existing_trades if t.get("ibkr_trade_id")
    }

    new_trades = [t for t in ibkr_trades if t.get("ibkr_trade_id") not in existing_ids]
    skipped = len(ibkr_trades) - len(new_trades)

    if skipped:
        console.print(f"[dim]Skipping {skipped} already-imported trade(s).[/dim]\n")

    if not new_trades:
        console.print("[green]All trades already imported. Nothing new to add.[/green]")
        return

    # --- Preview table ---
    prev = Table(box=box.ROUNDED, title=f"[bold]New Trades to Import ({len(new_trades)})[/bold]", show_lines=True)
    prev.add_column("Ticker", style="cyan bold", width=8)
    prev.add_column("Direction", justify="center", width=10)
    prev.add_column("Shares", justify="right", width=8)
    prev.add_column("Entry", justify="right", width=9)
    prev.add_column("Exit", justify="right", width=9)
    prev.add_column("P&L", justify="right", width=11)
    prev.add_column("P&L %", justify="right", width=8)
    prev.add_column("Outcome", justify="center", width=10)
    prev.add_column("Exit Date", width=12)
    prev.add_column("CCY", width=5)

    for t in new_trades:
        pnl = t.get("pnl", 0)
        pnl_pct = t.get("pnl_pct", 0)
        direction = t.get("direction", "?")
        outcome = t.get("outcome", "?")
        pnl_color = "green" if pnl >= 0 else "red"
        dir_color = {"BUY": "green", "SHORT": "red"}.get(direction, "white")
        out_color = {"WIN": "green", "LOSS": "red", "BREAK_EVEN": "yellow"}.get(outcome, "white")
        prev.add_row(
            t.get("ticker", "?"),
            f"[{dir_color}]{direction}[/{dir_color}]",
            f"{t.get('shares', 0):.0f}",
            f"${t.get('entry_price', 0):.2f}",
            f"${t.get('exit_price', 0):.2f}",
            f"[{pnl_color}]${pnl:+.2f}[/{pnl_color}]",
            f"[{pnl_color}]{pnl_pct:+.1f}%[/{pnl_color}]",
            f"[{out_color}]{outcome}[/{out_color}]",
            t.get("exit_date", "?"),
            t.get("currency", "USD"),
        )
    console.print(prev)

    # --- Confirm import ---
    console.print()
    confirm = typer.prompt("Import these trades into trades.json?", default="Y").strip().upper()
    if confirm not in ("Y", "YES", ""):
        console.print("[yellow]Import cancelled.[/yellow]")
        return

    # --- Build full trade entries ---
    now = datetime.datetime.now().isoformat()
    added = 0
    for t in new_trades:
        ticker = t["ticker"]
        sector = _fetch_sector(ticker)
        entry = {
            "ticker": ticker,
            "sector": sector,
            "direction": t["direction"],
            "shares": t["shares"],
            "entry_price": t["entry_price"],
            "exit_price": t["exit_price"],
            "pnl": t["pnl"],
            "pnl_pct": t["pnl_pct"],
            "outcome": t["outcome"],
            "prediction_accuracy": None,
            "beat_prediction_correct": None,
            "guidance_prediction_correct": None,
            "key_lesson": "",
            "trade_date": None,          # entry date not available from Flex close record
            "exit_date": t["exit_date"],
            "screening_run": None,
            "analysis_path": None,
            "reflection_path": None,
            "source": "ibkr",
            "currency": t.get("currency", "USD"),
            "ibkr_trade_id": t.get("ibkr_trade_id"),
            "ibkr_exec_id": t.get("ibkr_exec_id"),
            "logged_at": now,
        }
        existing_trades.append(entry)
        added += 1

    trade_log_path.write_text(_json.dumps(existing_trades, indent=2), encoding="utf-8")
    console.print(f"\n[green]✓ Imported {added} trade(s) → {trade_log_path}[/green]")
    console.print(
        "[dim]Note: trade_date (entry date) is not available from the Flex closing record. "
        "Run 'reflect' on any trade to add full analysis context.[/dim]\n"
    )


@app.command("allocation-weights")
def allocation_weights(
    beat:     Optional[float] = typer.Option(None, "--beat",     help="Weight for beat score bucket"),
    guidance: Optional[float] = typer.Option(None, "--guidance", help="Weight for guidance score bucket"),
    setup:    Optional[float] = typer.Option(None, "--setup",    help="Weight for setup score bucket"),
    reset:    bool            = typer.Option(False, "--reset",   help="Reset all weights to 1.0"),
):
    """View or update the scoring weights used by the Allocation Manager.

    Weights scale each analysis bucket (beat, guidance, setup) when computing
    the weighted_score that the AI council uses for sizing decisions.
    A weight > 1.0 amplifies that bucket; < 1.0 dampens it.

    Examples:\n
      tradingagents allocation-weights               # show current weights\n
      tradingagents allocation-weights --beat 1.5    # trust beat predictions more\n
      tradingagents allocation-weights --guidance 0.7 --setup 1.2\n
      tradingagents allocation-weights --reset
    """
    from tradingagents.allocation.weights import load_weights, save_weights

    if reset:
        save_weights(1.0, 1.0, 1.0)
        console.print("[green]Weights reset to 1.0 / 1.0 / 1.0[/green]")
        return

    current = load_weights()

    if beat is None and guidance is None and setup is None:
        # Display only
        w_tbl = Table(box=box.ROUNDED, title="[bold]Allocation Scoring Weights[/bold]", show_lines=True)
        w_tbl.add_column("Bucket",      style="cyan", width=12)
        w_tbl.add_column("Weight",      justify="right", width=8)
        w_tbl.add_column("Effect",      width=40)
        w_tbl.add_row("beat",     f"{current['beat']:.2f}",     "EPS beat prediction confidence")
        w_tbl.add_row("guidance", f"{current['guidance']:.2f}", "Forward guidance tone confidence")
        w_tbl.add_row("setup",    f"{current['setup']:.2f}",    "Technical / fundamental pre-earnings setup")
        console.print(w_tbl)
        console.print(
            "\n[dim]weighted_score = beat_w × beat_score + guidance_w × guidance_score + setup_w × setup_score[/dim]"
        )
        console.print(
            "[dim]Adjust weights based on calibration results to reflect which bucket is most predictive.[/dim]\n"
        )
        return

    # Update supplied values, keep others unchanged
    new_beat     = beat     if beat     is not None else current["beat"]
    new_guidance = guidance if guidance is not None else current["guidance"]
    new_setup    = setup    if setup    is not None else current["setup"]

    # Validate
    for name, val in [("beat", new_beat), ("guidance", new_guidance), ("setup", new_setup)]:
        if val < 0:
            console.print(f"[red]Weight for '{name}' must be ≥ 0 (got {val}).[/red]")
            raise typer.Exit(1)

    save_weights(new_beat, new_guidance, new_setup)
    console.print(
        f"[green]Weights updated:[/green] "
        f"beat=[cyan]{new_beat:.2f}[/cyan]  "
        f"guidance=[cyan]{new_guidance:.2f}[/cyan]  "
        f"setup=[cyan]{new_setup:.2f}[/cyan]"
    )


# ── Static reports site builder ───────────────────────────────────────────────

def _extract_brief_scores(brief_md: str) -> dict:
    """Parse the JSON score block at the bottom of an earnings_brief.md."""
    import re as _re
    import json as _json
    m = _re.search(r'```json\s*(\{.*?\})\s*```', brief_md, _re.DOTALL)
    if not m:
        return {}
    try:
        return _json.loads(m.group(1))
    except Exception:
        return {}


def _build_reports_data(reports_dir: Path, trades_path: Path) -> dict:
    """Collect all report data into a single dict for the static reports site."""
    import json as _json
    import datetime as _dt

    # ── Trades ────────────────────────────────────────────────────────────────
    trades: list = []
    if trades_path.exists():
        try:
            trades = _json.loads(trades_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # ── Screening runs ────────────────────────────────────────────────────────
    screening_runs: list = []
    for d in sorted(reports_dir.glob("screening_*/"), reverse=True):
        if not d.is_dir():
            continue
        name = d.name
        parts = name.split("_")
        date_str = parts[1] if len(parts) > 1 else ""

        table_md = alloc_md = None
        table_path = d / "screening_table.md"
        alloc_path = d / "allocation.md"
        if table_path.exists():
            raw = table_path.read_text(encoding="utf-8")
            table_md = raw[:80_000] if len(raw) > 80_000 else raw
        if alloc_path.exists():
            raw = alloc_path.read_text(encoding="utf-8")
            alloc_md = raw[:60_000] if len(raw) > 60_000 else raw

        cal_summary = None
        cal_path = d / "calibration.json"
        if cal_path.exists():
            try:
                cal_data = _json.loads(cal_path.read_text(encoding="utf-8"))
                rows = cal_data.get("rows", [])
                if rows:
                    n_cal = len(rows)
                    sig_ok = sum(1 for r in rows if r.get("signal_correct") is True)
                    beat_ok = sum(1 for r in rows if r.get("beat_prediction_correct") is True)
                    beat_n = sum(1 for r in rows if r.get("beat_prediction_correct") is not None)
                    cal_summary = {
                        "signal_accuracy_pct": sig_ok / n_cal * 100 if n_cal else None,
                        "beat_accuracy_pct": beat_ok / beat_n * 100 if beat_n else None,
                        "n": n_cal,
                    }
            except Exception:
                pass

        tickers: list = []
        for td in sorted(d.iterdir()):
            if not td.is_dir():
                continue
            brief_path = td / "earnings_brief.md"
            if not brief_path.exists():
                continue
            brief_raw = brief_path.read_text(encoding="utf-8")
            scores = _extract_brief_scores(brief_raw)
            capped = brief_raw[:15_000] if len(brief_raw) > 15_000 else brief_raw
            pm_path = td / "5_portfolio" / "decision.md"
            pm_md = None
            if pm_path.exists():
                raw_pm = pm_path.read_text(encoding="utf-8")
                pm_md = raw_pm[:12_000] if len(raw_pm) > 12_000 else raw_pm
            tickers.append({
                "ticker":               td.name,
                "signal":               scores.get("signal"),
                "confidence":           scores.get("confidence"),
                "beat_score":           scores.get("beat_score"),
                "guidance_score":       scores.get("guidance_score"),
                "setup_score":          scores.get("setup_score"),
                "total_score":          scores.get("total_score"),
                "one_liner":            scores.get("one_liner"),
                "earnings_brief_md":    capped,
                "portfolio_decision_md": pm_md,
            })

        screening_runs.append({
            "id":                 name,
            "date":               date_str,
            "n_tickers":          len(tickers),
            "screening_table_md": table_md,
            "allocation_md":      alloc_md,
            "calibration":        cal_summary,
            "tickers":            tickers,
        })

    # ── Standalone analyses ───────────────────────────────────────────────────
    standalone: list = []
    _screening_names = {d.name for d in reports_dir.glob("screening_*/") if d.is_dir()}
    for d in sorted(reports_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        if d.name.startswith("screening_") or d.name == "reflections" or d.name == "web":
            continue
        if d.name in _screening_names:
            continue
        brief_path = d / "earnings_brief.md"
        if not brief_path.exists():
            continue
        brief_raw = brief_path.read_text(encoding="utf-8")
        parts2 = d.name.split("_")
        ticker2 = parts2[0] if parts2 else d.name
        raw_date = parts2[1] if len(parts2) > 1 else ""
        date2 = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) >= 8 else ""
        capped = brief_raw[:15_000] if len(brief_raw) > 15_000 else brief_raw
        standalone.append({
            "id":               d.name,
            "ticker":           ticker2,
            "date":             date2,
            "earnings_brief_md": capped,
        })

    # ── Reflections ───────────────────────────────────────────────────────────
    reflections: list = []
    reflections_dir = reports_dir / "reflections"
    if reflections_dir.exists():
        seen_refl: set = set()
        for d in sorted(reflections_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            pm_path = d / "post_mortem.md"
            if not pm_path.exists():
                continue
            parts3 = d.name.split("_")
            if len(parts3) < 2:
                continue
            ticker3 = parts3[0]
            exit_date3 = parts3[1] if len(parts3) > 1 else ""
            key3 = (ticker3, exit_date3)
            if key3 in seen_refl:
                continue
            seen_refl.add(key3)
            pm_raw = pm_path.read_text(encoding="utf-8")
            score3 = _extract_brief_scores(pm_raw) or {}
            # parse_reflection_score is richer — re-use it
            try:
                from tradingagents.reflection.layer import parse_reflection_score as _prs
                score3 = _prs(pm_raw) or {}
            except Exception:
                pass
            reflections.append({
                "id":            d.name,
                "ticker":        ticker3,
                "exit_date":     exit_date3,
                "outcome":       score3.get("outcome"),
                "key_lesson":    score3.get("key_lesson", ""),
                "post_mortem_md": pm_raw[:30_000] if len(pm_raw) > 30_000 else pm_raw,
            })

    # ── Stats ─────────────────────────────────────────────────────────────────
    stats: dict = {}
    if trades:
        n_t = len(trades)
        wins_t  = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        losses_t = sum(1 for t in trades if (t.get("pnl") or 0) < 0)
        total_pnl = sum(t.get("pnl") or 0 for t in trades)
        stats["wins"] = wins_t
        stats["losses"] = losses_t
        stats["total_pnl"] = total_pnl
        stats["win_rate"] = wins_t / n_t * 100 if n_t else 0

        # Consolidate fills → positions for capital calculations
        _pos: dict = {}
        for t in trades:
            key = (t.get("ticker", ""), t.get("exit_date", ""))
            sh = t.get("shares") or 0
            ep = t.get("entry_price") or 0
            if key not in _pos:
                _pos[key] = {"exit_date": key[1], "_sh": sh, "_ep_w": ep * sh, "_pnl": t.get("pnl") or 0}
            else:
                g = _pos[key]
                g["_sh"] += sh
                g["_ep_w"] += ep * sh
                g["_pnl"] += t.get("pnl") or 0

        _daily: dict = {}
        for g in _pos.values():
            d2 = g["exit_date"]
            if not d2:
                continue
            sh2 = g["_sh"]
            ep2 = g["_ep_w"] / sh2 if sh2 > 0 else 0
            cost = ep2 * sh2
            if d2 not in _daily:
                _daily[d2] = {"capital": 0.0, "pnl": 0.0}
            _daily[d2]["capital"] += cost
            _daily[d2]["pnl"] += g["_pnl"]

        for dd in _daily.values():
            dd["ret_pct"] = dd["pnl"] / dd["capital"] * 100 if dd["capital"] > 0 else 0.0

        dates_sorted = sorted(_daily.keys())
        n_days = len(dates_sorted)
        first_date = dates_sorted[0] if dates_sorted else None
        avg_daily_cap = sum(dd["capital"] for dd in _daily.values()) / n_days if n_days else 0

        stats["daily_capital"] = _daily
        stats["n_trading_days"] = n_days
        stats["first_date"] = first_date
        stats["last_date"] = dates_sorted[-1] if dates_sorted else None
        stats["avg_daily_capital"] = avg_daily_cap
        stats["return_pct"] = total_pnl / avg_daily_cap * 100 if avg_daily_cap > 0 else None
        stats["today"] = _dt.date.today().isoformat()

        # Benchmark (non-fatal if yfinance unavailable)
        if first_date and avg_daily_cap > 0:
            bench: dict = {}
            try:
                import yfinance as _yf2
                for sym in ("QQQ", "SPY"):
                    try:
                        df = _yf2.download(sym, start=first_date, end=stats["today"], progress=False, auto_adjust=True)
                        if df.empty:
                            continue
                        close = df["Close"].squeeze() if hasattr(df["Close"], "squeeze") else df["Close"]
                        p0 = float(close.iloc[0])
                        p1 = float(close.iloc[-1])
                        ret = (p1 - p0) / p0
                        bench[sym] = {
                            "price_start": round(p0, 2),
                            "price_end":   round(p1, 2),
                            "ret_pct":     round(ret * 100, 4),
                            "pnl":         avg_daily_cap * ret,
                        }
                    except Exception:
                        pass
            except ImportError:
                pass
            stats["benchmark"] = bench

    return {
        "generated_at":        _dt.datetime.now().isoformat(),
        "trades":               trades,
        "screening_runs":       screening_runs,
        "standalone_analyses":  standalone,
        "reflections":          reflections,
        "stats":                stats,
    }


def _write_reports_site(reports_dir: Path, trades_path: Path) -> Path:
    """Build reports/web/index.html with all report data embedded."""
    import json as _json

    template_path = Path(__file__).parent / "static" / "reports_site.html"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    data = _build_reports_data(reports_dir, trades_path)
    template = template_path.read_text(encoding="utf-8")
    payload = _json.dumps(data, ensure_ascii=False, default=str)
    html = template.replace("__TRADINGAGENTS_DATA__", payload, 1)

    out_dir = reports_dir / "web"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def _auto_build_web() -> None:
    """Silently rebuild reports/web/index.html after any data-generating command."""
    try:
        rp = Path("reports")
        if not rp.exists():
            return
        tp = Path.home() / ".tradingagents" / "trades.json"
        _write_reports_site(rp, tp)
    except Exception:
        pass


@app.command("build-web")
def build_web():
    """Build a static reports website at reports/web/index.html.

    Embeds all report data (trades, screenings, reflections, calibration)
    into a single HTML file you can open in any browser — no server needed.
    """
    reports_dir = Path("reports")
    trades_path = Path.home() / ".tradingagents" / "trades.json"

    if not reports_dir.exists():
        console.print("[yellow]No reports/ directory found. Run a screen first.[/yellow]")
        raise typer.Exit(1)

    console.print("[dim]Building reports site…[/dim]")
    with console.status("[dim]Collecting data and fetching benchmark prices…[/dim]"):
        try:
            out_path = _write_reports_site(reports_dir, trades_path)
        except Exception as e:
            console.print(f"[red]Error building site: {e}[/red]")
            raise typer.Exit(1)

    console.print(f"[green]✓ Built:[/green] {out_path.resolve()}")
    console.print("[dim]Open that file in any browser — no server needed.[/dim]")


@app.command()
def dashboard(
    port: int = typer.Option(8765, "--port", "-p", help="Port to serve on (default 8765)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open browser automatically"),
):
    """Launch the TradingAgents web dashboard in your browser."""
    import json as _json
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    trades_path = Path.home() / ".tradingagents" / "trades.json"
    html_path = Path(__file__).parent / "static" / "dashboard.html"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/trades":
                data = []
                if trades_path.exists():
                    try:
                        data = _json.loads(trades_path.read_text(encoding="utf-8"))
                    except Exception:
                        data = []
                body = _json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            elif self.path in ("/", "/index.html"):
                try:
                    body = html_path.read_bytes()
                except FileNotFoundError:
                    body = b"<h1>dashboard.html not found</h1>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # suppress per-request logs

    url = f"http://127.0.0.1:{port}"
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        console.print(f"[red]Port {port} is already in use. Try --port <other_port>.[/red]")
        return

    console.print(f"[green]Dashboard running at {url}[/green]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    if not no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]Dashboard stopped.[/yellow]")


if __name__ == "__main__":
    app()

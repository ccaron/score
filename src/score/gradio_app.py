"""Gradio app for displaying hockey schedules"""

import gradio as gr
from bs4 import BeautifulSoup
import pandas as pd
from pathlib import Path


def parse_schedule_html(html_path: str):
    """Parse the schedule HTML file and extract data."""
    with open(html_path, 'r') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    # Extract statistics
    stats = {}
    stat_boxes = soup.find_all('div', class_='stat-box')
    for box in stat_boxes:
        value = box.find('div', class_='stat-value').text.strip()
        label = box.find('div', class_='stat-label').text.strip()
        stats[label] = value

    # Extract schedule data
    schedule_data = []
    date_sections = soup.find_all('div', class_='date-section')
    for section in date_sections:
        date = section.find('div', class_='date-header').text.strip()
        games = section.find_all('div', class_='game-line')

        for game in games:
            if 'unused' not in game.get('class', []):
                time = game.find('div', class_='game-time').text.strip()
                sheet = game.find('div', class_='game-sheet').text.strip()
                matchup = game.find('div', class_='game-matchup').text.strip()

                schedule_data.append({
                    'Date': date,
                    'Time': time,
                    'Sheet': sheet,
                    'Matchup': matchup
                })

        # Get bye teams
        bye_section = section.find('div', class_='bye-section')
        if bye_section:
            bye_teams = [team.text.strip() for team in bye_section.find_all('span', class_='bye-team')]
            if bye_teams:
                schedule_data.append({
                    'Date': date,
                    'Time': 'BYE',
                    'Sheet': '',
                    'Matchup': ', '.join(bye_teams)
                })

    # Extract fairness metrics
    home_away_data = []
    time_slot_data = []
    sheet_data = []

    metric_cards = soup.find_all('div', class_='metric-card')
    for card in metric_cards:
        title = card.find('h3').text.strip()
        bar_rows = card.find_all('div', class_='bar-row')

        if 'Home/Away' in title:
            for row in bar_rows:
                team = row.find('div', class_='bar-label').text.strip()
                bars = row.find_all('div', class_='bar')
                if len(bars) >= 2:
                    home = bars[0].text.strip()
                    away = bars[1].text.strip()
                    home_away_data.append({'Team': team, 'Home': home, 'Away': away})

        elif 'Time Slot' in title:
            for row in bar_rows:
                team = row.find('div', class_='bar-label').text.strip()
                bars = row.find_all('div', class_='bar')
                slot_counts = {}
                for bar in bars:
                    slot = bar.get('title', '')
                    count = bar.text.strip()
                    if slot:
                        slot_counts[slot] = count
                time_slot_data.append({'Team': team, **slot_counts})

        elif 'Ice Sheet' in title:
            for row in bar_rows:
                team = row.find('div', class_='bar-label').text.strip()
                bars = row.find_all('div', class_='bar')
                sheet_counts = {}
                for bar in bars:
                    sheet_name = bar.get('title', '')
                    count = bar.text.strip()
                    if sheet_name:
                        sheet_counts[sheet_name] = count
                sheet_data.append({'Team': team, **sheet_counts})

    return stats, schedule_data, home_away_data, time_slot_data, sheet_data


def create_gradio_app(html_path: str = "schedule.html"):
    """Create the Gradio interface for the schedule."""

    # Parse the HTML
    stats, schedule_data, home_away_data, time_slot_data, sheet_data = parse_schedule_html(html_path)

    # Convert to DataFrames
    schedule_df = pd.DataFrame(schedule_data)
    home_away_df = pd.DataFrame(home_away_data) if home_away_data else None
    time_slot_df = pd.DataFrame(time_slot_data) if time_slot_data else None
    sheet_df = pd.DataFrame(sheet_data) if sheet_data else None

    # Create the interface
    with gr.Blocks(title="Hockey Schedule", theme=gr.themes.Soft()) as app:
        gr.Markdown("# 🏒 Hockey Schedule")
        gr.Markdown(f"**{stats.get('Total Games', 'N/A')} Games** across **{stats.get('Game Days', 'N/A')} Days** with **{stats.get('Teams', 'N/A')} Teams**")

        # Statistics
        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown(f"### Total Games\n## {stats.get('Total Games', 'N/A')}")
            with gr.Column(scale=1):
                gr.Markdown(f"### Game Days\n## {stats.get('Game Days', 'N/A')}")
            with gr.Column(scale=1):
                gr.Markdown(f"### Ice Utilization\n## {stats.get('Ice Utilization', 'N/A')}")
            with gr.Column(scale=1):
                gr.Markdown(f"### Teams\n## {stats.get('Teams', 'N/A')}")

        # Tabs for different views
        with gr.Tabs():
            with gr.Tab("📅 Schedule"):
                gr.Markdown("### Complete Season Schedule")
                gr.Dataframe(
                    schedule_df,
                    interactive=False,
                    wrap=True,
                    column_widths=["30%", "10%", "15%", "45%"]
                )

            with gr.Tab("📊 Fairness Metrics"):
                gr.Markdown("### Home/Away Balance")
                if home_away_df is not None:
                    gr.Dataframe(home_away_df, interactive=False)

                gr.Markdown("### Time Slot Distribution")
                if time_slot_df is not None:
                    gr.Dataframe(time_slot_df, interactive=False)

                gr.Markdown("### Ice Sheet Distribution")
                if sheet_df is not None:
                    gr.Dataframe(sheet_df, interactive=False)

            with gr.Tab("🔍 Filter Schedule"):
                with gr.Row():
                    team_filter = gr.Dropdown(
                        choices=['All'] + [str(i) for i in range(1, 10)],
                        value='All',
                        label="Filter by Team"
                    )
                    date_filter = gr.Dropdown(
                        choices=['All'] + sorted(schedule_df['Date'].unique().tolist()),
                        value='All',
                        label="Filter by Date"
                    )

                filtered_output = gr.Dataframe(
                    schedule_df,
                    interactive=False,
                    wrap=True,
                    column_widths=["30%", "10%", "15%", "45%"]
                )

                def filter_schedule(team, date):
                    df = schedule_df.copy()
                    if team != 'All':
                        df = df[df['Matchup'].str.contains(team)]
                    if date != 'All':
                        df = df[df['Date'] == date]
                    return df

                team_filter.change(
                    filter_schedule,
                    inputs=[team_filter, date_filter],
                    outputs=filtered_output
                )
                date_filter.change(
                    filter_schedule,
                    inputs=[team_filter, date_filter],
                    outputs=filtered_output
                )

    return app


def main():
    """Main entry point for the Gradio app."""
    html_path = Path(__file__).parent.parent.parent / "schedule.html"

    if not html_path.exists():
        print(f"Error: schedule.html not found at {html_path}")
        print("Please generate a schedule first using: uv run score-schedule examples/schedule.yaml --html schedule.html")
        return

    app = create_gradio_app(str(html_path))
    app.launch(server_name="0.0.0.0", server_port=7860, share=False)


if __name__ == "__main__":
    main()

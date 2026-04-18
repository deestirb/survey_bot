# database.py
import sqlite3
from questions import QUESTIONS

DB_FILE = "/data/survey_results.db"
# ↑ This path is for Railway (persistent volume).
#   For local testing, change to: DB_FILE = "survey_results.db"


def init_db():
    """Create the database and responses table if they don't exist yet."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Build one answer column + one timing column per question, dynamically
    dynamic_cols = []
    for i in range(len(QUESTIONS)):
        dynamic_cols.append(f"q{i+1}_answer TEXT")
        dynamic_cols.append(f"q{i+1}_seconds REAL")
    dynamic_cols_sql = ",\n    ".join(dynamic_cols)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS responses (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER,
            username        TEXT,
            first_name      TEXT,
            condition       TEXT,       -- 'bot' or 'web'
            start_time      TEXT,
            end_time        TEXT,
            total_seconds   REAL,
            completed       INTEGER DEFAULT 0,
            {dynamic_cols_sql}
        )
    """)
    conn.commit()
    conn.close()
    print("Database ready.")


def create_response_row(user_id, username, first_name, start_time, condition="bot"):
    """
    Insert a new row when a participant starts (either condition).
    Returns the row ID so we can update it as answers come in.
    Web-condition rows will have completed=0 and no answer columns filled —
    this is intentional so you can count redirects vs completions.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO responses (user_id, username, first_name, start_time, condition)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, username, first_name, start_time, condition))
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id


def save_answer(row_id, question_index, answer, seconds_spent):
    """
    Update a specific question's answer and time spent for an existing row.
    question_index is 0-based (question 1 = index 0 → column q1_answer).
    """
    q_num = question_index + 1
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(f"""
        UPDATE responses
        SET q{q_num}_answer = ?, q{q_num}_seconds = ?
        WHERE id = ?
    """, (answer, round(seconds_spent, 2), row_id))
    conn.commit()
    conn.close()


def finalize_response(row_id, end_time, total_seconds):
    """Mark a bot-condition survey as complete and record the end time."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE responses
        SET end_time = ?, total_seconds = ?, completed = 1
        WHERE id = ?
    """, (end_time, round(total_seconds, 2), row_id))
    conn.commit()
    conn.close()


def get_stats():
    """
    Return stats for the /stats admin command.
    Returns: (bot_completed, bot_started, web_redirected, avg_time_seconds)
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM responses WHERE condition = 'bot' AND completed = 1")
    bot_completed = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM responses WHERE condition = 'bot' AND completed = 0")
    bot_started = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM responses WHERE condition = 'web'")
    web_redirected = cursor.fetchone()[0]

    cursor.execute("SELECT AVG(total_seconds) FROM responses WHERE condition = 'bot' AND completed = 1")
    avg_time = cursor.fetchone()[0]

    conn.close()
    return bot_completed, bot_started, web_redirected, avg_time

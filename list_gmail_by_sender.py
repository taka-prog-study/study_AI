import base64
import csv
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path("credentials.json")
TOKEN_FILE = Path("token.json")


def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            SCOPES,
        )
        creds = flow.run_local_server(port=0)

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def get_header(headers, name):
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def extract_body(payload):
    """Gmailのペイロード構造から本文（プレーンテキスト優先）をデコードして取得"""
    body_text = ""

    # 単一パート構造の場合
    if "body" in payload and "data" in payload["body"]:
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data.encode("UTF-8")).decode("utf-8", errors="replace")

    # マルチパート構造（添付ファイルやHTML混在）の場合
    parts = payload.get("parts", [])
    for part in parts:
        mime_type = part.get("mimeType", "")
        if mime_type == "text/plain" and "data" in part.get("body", {}):
            data = part["body"]["data"]
            return base64.urlsafe_b64decode(data.encode("UTF-8")).decode("utf-8", errors="replace")
        elif "parts" in part:
            # ネストされたマルチパートの再帰検索
            body_text = extract_body(part)
            if body_text:
                return body_text

    # プレーンテキストがない場合はHTMLパートを取得
    for part in parts:
        if part.get("mimeType") == "text/html" and "data" in part.get("body", {}):
            data = part["body"]["data"]
            return base64.urlsafe_b64decode(data.encode("UTF-8")).decode("utf-8", errors="replace")

    return body_text


def sanitize_filename(filename):
    """Windowsでファイル名に使えない特殊文字を置換"""
    return re.sub(r'[\\/:*?"<>|]', "_", filename)


def collect_and_save_emails(sender_email):
    service = get_gmail_service()
    query = f"from:{sender_email}"
    page_token = None
    message_ids = []

    print("検索条件に該当するメールを検索中...")
    while True:
        result = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=500,
            pageToken=page_token,
        ).execute()

        refs = result.get("messages", [])
        if not refs:
            break
        message_ids.extend([ref["id"] for ref in refs])

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    total_count = len(message_ids)
    if total_count == 0:
        print("該当するメールが見つかりませんでした。")
        return

    # 保存用フォルダの作成 (例: output_emails_example@gmail.com)
    safe_sender_name = sanitize_filename(sender_email)
    output_dir = Path(f"output_emails_{safe_sender_name}")
    output_dir.mkdir(exist_ok=True)

    print(f"対象メール: 合計 {total_count} 件を発見。")
    print(f"保存先フォルダ: 「{output_dir.resolve()}」\n")

    csv_file_path = output_dir / "all_messages_summary.csv"
    total_processed = 0

    # CSV出力用のファイルを開く
    with open(csv_file_path, mode="w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["No", "ID", "Date", "From", "Subject", "Body"])

        # 100件ずつ高速バッチ取得
        chunk_size = 100
        for i in range(0, total_count, chunk_size):
            chunk = message_ids[i:i + chunk_size]
            
            def callback(request_id, response, exception):
                nonlocal total_processed
                if exception is None:
                    payload = response.get("payload", {})
                    headers = payload.get("headers", [])

                    msg_id = response.get("id", "")
                    msg_from = get_header(headers, "From")
                    msg_subject = get_header(headers, "Subject") or "(件名なし)"
                    msg_date = get_header(headers, "Date")
                    msg_body = extract_body(payload)

                    total_processed += 1

                    # 1. CSVへ1行追加
                    writer.writerow([total_processed, msg_id, msg_date, msg_from, msg_subject, msg_body])

                    # 2. 個別テキストファイル (.txt) としてフォルダへ保存
                    safe_subject = sanitize_filename(msg_subject)[:50]  # 長すぎる件名をカット
                    txt_filename = f"{total_processed:04d}_{safe_subject}.txt"
                    txt_path = output_dir / txt_filename

                    txt_content = (
                        f"件名: {msg_subject}\n"
                        f"送信者: {msg_from}\n"
                        f"日時: {msg_date}\n"
                        f"ID: {msg_id}\n"
                        f"{'='*50}\n\n"
                        f"{msg_body}"
                    )
                    txt_path.write_text(txt_content, encoding="utf-8")

                    print(f"[{total_processed}/{total_count}] 保存完了: {txt_filename}")

            batch = service.new_batch_http_request(callback=callback)
            for msg_id in chunk:
                # 本文（payload）を取得するため format="full" に指定
                req = service.users().messages().get(
                    userId="me",
                    id=msg_id,
                    format="full",
                )
                batch.add(req)

            batch.execute()

    print(f"\nすべての処理が完了しました！")
    print(f"フォルダ「{output_dir.name}」の中に個別テキストファイルとCSV一覧が作成されています。")


if __name__ == "__main__":
    sender = input("検索する From アドレス: ").strip()
    if not sender:
        raise SystemExit("メールアドレスを入力してください。")

    collect_and_save_emails(sender)
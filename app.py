from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import os
import hashlib
import base64
import re
from cryptography.fernet import Fernet

app = Flask(__name__)

# 플라스크 세션 암호화 키 설정
app.secret_key = "appez_schedule_secure_secret_key"

# ★ 세션 유휴 유효 시간 설정 (단위: 분)
SESSION_TIMEOUT_MINUTES = 10

@app.before_request
def make_session_permanent():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(minutes=SESSION_TIMEOUT_MINUTES)
    session.modified = True

@app.after_request
def add_no_cache_headers(response):
    if response.content_type and "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

# 개인정보 암·복호화 키 설정 (운영 중 절대 임의 변경 금지)
ENCRYPTION_SECRET = os.environ.get("ENCRYPTION_SECRET", "appez_pii_secure_key_2026!@#")
_fernet_key = base64.urlsafe_b64encode(hashlib.sha256(ENCRYPTION_SECRET.encode('utf-8')).digest())
cipher_suite = Fernet(_fernet_key)

def encrypt_pii(plain_text):
    if plain_text is None:
        return ""
    text = str(plain_text).strip()
    if not text:
        return ""
    if text.startswith("gAAAAAB"):
        return text
    return cipher_suite.encrypt(text.encode('utf-8')).decode('utf-8')

def decrypt_pii(cipher_text):
    if cipher_text is None:
        return ""
    text = str(cipher_text).strip()
    if not text:
        return ""
    try:
        return cipher_suite.decrypt(text.encode('utf-8')).decode('utf-8')
    except Exception:
        return text

def get_google_sheet(sheet_name):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client_gs = gspread.authorize(creds)
    doc = client_gs.open("automation") 
    
    try:
        sheet = doc.worksheet(sheet_name)
    except Exception:
        sheet = doc.add_worksheet(title=sheet_name, rows=1000, cols=12)
        sheet.append_row(["업체명", "이름", "성별", "연락처", "생년월일", "이메일", "점검일자", "점검시간", "상태", "비고"])
        
    return sheet

def mask_phone(phone_str):
    phone = str(phone_str).replace('-', '').strip()
    if len(phone) == 10 and phone.startswith('10'):
        phone = '0' + phone
    if len(phone) == 11:
        return f"{phone[:3]}-****-{phone[7:]}"
    elif len(phone) == 10:
        return f"{phone[:3]}-***-{phone[6:]}"
    return phone

def format_phone(phone_str):
    phone = str(phone_str).replace('-', '').strip()
    if len(phone) == 10 and phone.startswith('10'):
        phone = '0' + phone
    if len(phone) == 11:
        return f"{phone[:3]}-{phone[3:7]}-{phone[7:]}"
    elif len(phone) == 10:
        return f"{phone[:3]}-{phone[3:6]}-{phone[6:]}"
    return str(phone_str).strip()

def format_birthdate(birth_str):
    birth = str(birth_str).replace('-', '').strip()
    if len(birth) == 8:
        return f"{birth[:4]}-{birth[4:6]}-{birth[6:]}"
    return birth_str

    # ... (기존 format_birthdate 함수 아래에 추가) ...

def normalize_company_name(company_str):
    name = str(company_str or "").strip()
    if not name:
        return ""
    # OCR 오인식 보정: 펜타링크(주) / 펜타링크 -> 펜타린크(주)
    if name in ("펜타링크(주)", "펜타링크"):
        return "펜타린크(주)"
    return name.replace("펜타링크", "펜타린크")

# ==================== [1] 통합 진입점(localhost:5000) 및 일반 사용자 라우트 ====================

@app.route('/')
def index():
    now = datetime.now()
    current_m = f"{now.month}월"
    next_m = f"{(now + relativedelta(months=1)).month}월"
    return render_template('index.html', current_month=current_m, next_month=next_m)

@app.route('/check-schedule', methods=['POST'])
def check_schedule():
    data = request.get_json()
    if not data:
        return jsonify({'status': 'error', 'message': '요청 데이터가 없습니다.'})
        
    pin = str(data.get('pin', '')).strip()
    target_month = str(data.get('target_month', '')).strip()

    if not pin:
        return jsonify({'status': 'error', 'message': 'PIN 번호를 입력해주세요.'})

    try:
        company_sheet = get_google_sheet("업체")
        raw_records = company_sheet.get_all_records()
        
        matched_company = None
        fixed_time = ''
        for raw_row in raw_records:
            row = {str(k).strip(): v for k, v in raw_row.items()}
            raw_sheet_pin = str(row.get('PIN', row.get('비밀번호', ''))).strip()
            sheet_pin = decrypt_pii(raw_sheet_pin).strip()
            sheet_comp = str(row.get('업체명', '')).strip()

            is_pin_match = (sheet_pin == pin) or (
                sheet_pin.isdigit() and pin.isdigit() and len(pin) == 4 and sheet_pin.zfill(4) == pin
            )

            if is_pin_match:
                matched_company = sheet_comp
                fixed_time = str(row.get('점검시각', '')).strip()
                break

        if not matched_company:
            return jsonify({'status': 'error', 'message': '일치하는 PIN 번호가 없습니다.'})

        if matched_company.lower() in ("관리자", "admin"):
            session['admin_logged_in'] = True
            session.modified = True
            return jsonify({
                'status': 'admin_success',
                'redirect': url_for('admin_page')
            })

        if not target_month:
            return jsonify({'status': 'error', 'message': '조회 대상 월을 선택해주세요.'})

        session.pop('admin_logged_in', None)
        schedule_sheet = get_google_sheet(target_month)
        schedule_records = schedule_sheet.get_all_records()

        company_schedules = []
        for idx, raw_row in enumerate(schedule_records, start=2):
            row = {str(k).strip(): v for k, v in raw_row.items()}
            if str(row.get('업체명', '')).strip() == matched_company:
                dec_phone = decrypt_pii(row.get('연락처', ''))
                dec_birth = decrypt_pii(row.get('생년월일', ''))
                dec_email = decrypt_pii(row.get('이메일', ''))

                company_schedules.append({
                    'row_index': idx,
                    '업체명': matched_company,
                    '이름': str(row.get('이름', '')).strip(),
                    '성별': str(row.get('성별', '')).strip(),
                    '연락처': mask_phone(dec_phone),
                    'raw_phone': dec_phone,
                    '생년월일': format_birthdate(dec_birth),
                    'raw_birth': dec_birth,
                    '이메일': dec_email,
                    'raw_email': dec_email,
                    '점검일자': str(row.get('점검일자', '')).strip(),
                    '점검시간': str(row.get('점검시간', '')).strip(),
                    '상태': str(row.get('상태', '')).strip(),
                    '비고': str(row.get('비고', '')).strip()
                })

        return jsonify({
            'status': 'success',
            'company_name': matched_company,
            'selected_month': target_month,
            'fixed_time': fixed_time,
            'engineers': company_schedules
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'데이터 조회 중 오류 발생: {str(e)}'})

@app.route('/keep-alive', methods=['POST'])
def keep_alive():
    session.modified = True
    return jsonify({"status": "ok"})

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"status": "success", "message": "로그아웃되었습니다."})
        
@app.route('/confirm-schedule', methods=['POST'])
def confirm_schedule():
    data = request.get_json()
    company_name = data.get('company_name')
    target_month = data.get('target_month')
    change_type = data.get('change_type')
    row_index = data.get('row_index')
    person_data = data.get('person_data', {})
    target_engineer_name = data.get('target_engineer_name')
    reschedule_data = data.get('reschedule_data', {})

    try:
        sheet = get_google_sheet(target_month)
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
        col_idx_map = {col_name.strip(): i + 1 for i, col_name in enumerate(headers)}

        def find_target_row():
            if row_index:
                target_idx = int(row_index)
                for idx, row in enumerate(records, start=2):
                    if idx == target_idx and str(row.get('업체명', '')).strip() == company_name:
                        return idx
            for idx, row in enumerate(records, start=2):
                if str(row.get('업체명', '')).strip() == company_name and str(row.get('이름', '')).strip() == target_engineer_name:
                    return idx
            return None

        if change_type in ('confirm_engineer', 'confirm_all'):
            target_row_num = find_target_row()
            if target_row_num:
                if '상태' in col_idx_map:
                    sheet.update_cell(target_row_num, col_idx_map['상태'], '일정확인완료')
                return jsonify({'status': 'success', 'message': '해당 엔지니어의 일정이 확인 완료되었습니다.'})
            elif change_type == 'confirm_all':
                for idx, row in enumerate(records, start=2):
                    if str(row.get('업체명', '')).strip() == company_name:
                        if '상태' in col_idx_map:
                            sheet.update_cell(idx, col_idx_map['상태'], '일정확인완료')
                return jsonify({'status': 'success', 'message': '전체 일정이 확인 완료되었습니다.'})
            else:
                return jsonify({'status': 'error', 'message': '대상 엔지니어 일정을 찾지 못했습니다.'})

        elif change_type == 'add_engineer':
            # 기존 업체의 점검일자 및 점검시간 승계 (일자 변경은 일정 변경 요청 시에만 진행)
            default_date, default_time = '', ''
            for row in records:
                if str(row.get('업체명', '')).strip() == company_name:
                    default_date = str(row.get('점검일자', '')).strip()
                    default_time = str(row.get('점검시간', '')).strip()
                    break

            new_row = [None] * len(headers)
            if '업체명' in col_idx_map: new_row[col_idx_map['업체명'] - 1] = company_name
            if '이름' in col_idx_map: new_row[col_idx_map['이름'] - 1] = person_data.get('name', '')
            if '성별' in col_idx_map: new_row[col_idx_map['성별'] - 1] = person_data.get('gender', '')
            if '연락처' in col_idx_map: new_row[col_idx_map['연락처'] - 1] = encrypt_pii(person_data.get('phone', ''))
            if '생년월일' in col_idx_map: new_row[col_idx_map['생년월일'] - 1] = encrypt_pii(person_data.get('birth', ''))
            if '이메일' in col_idx_map: new_row[col_idx_map['이메일'] - 1] = encrypt_pii(person_data.get('email', ''))
            if '점검일자' in col_idx_map: new_row[col_idx_map['점검일자'] - 1] = default_date
            if '점검시간' in col_idx_map: new_row[col_idx_map['점검시간'] - 1] = default_time
            if '상태' in col_idx_map: new_row[col_idx_map['상태'] - 1] = '확인대기'
            
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if '비고' in col_idx_map:
                new_row[col_idx_map['비고'] - 1] = f"[{now_str}] 신규 엔지니어({person_data.get('name')}) 추가됨"

            sheet.append_row(new_row)
            return jsonify({'status': 'success', 'message': '엔지니어가 추가되었습니다.'})

        elif change_type == 'edit_engineer':
            target_row_num = find_target_row()
            if target_row_num:
                if '이름' in col_idx_map: sheet.update_cell(target_row_num, col_idx_map['이름'], person_data.get('name'))
                if '성별' in col_idx_map: sheet.update_cell(target_row_num, col_idx_map['성별'], person_data.get('gender'))
                if '연락처' in col_idx_map: sheet.update_cell(target_row_num, col_idx_map['연락처'], encrypt_pii(person_data.get('phone', '')))
                if '생년월일' in col_idx_map: sheet.update_cell(target_row_num, col_idx_map['생년월일'], encrypt_pii(person_data.get('birth', '')))
                if '이메일' in col_idx_map: sheet.update_cell(target_row_num, col_idx_map['이메일'], encrypt_pii(person_data.get('email', '')))
                
                if '비고' in col_idx_map:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    sheet.update_cell(target_row_num, col_idx_map['비고'], f"[{now_str}] 엔지니어({person_data.get('name')}) 정보 수정됨")
                return jsonify({'status': 'success', 'message': '엔지니어 정보가 수정되었습니다.'})
            else:
                return jsonify({'status': 'error', 'message': '수정할 엔지니어를 찾지 못했습니다.'})

        elif change_type == 'delete_engineer':
            target_row_num = find_target_row()
            if target_row_num:
                sheet.delete_rows(target_row_num)
                return jsonify({'status': 'success', 'message': '엔지니어가 삭제되었습니다.'})
            else:
                return jsonify({'status': 'error', 'message': '삭제할 엔지니어를 찾지 못했습니다.'})

        elif change_type == 'add_note':
            note_content = data.get('note_content', '')
            for idx, row in enumerate(records, start=2):
                if str(row.get('업체명', '')).strip() == company_name:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    current_note = str(row.get('비고', ''))
                    new_note = f"[{now_str}] {note_content} ({current_note})" if current_note else f"[{now_str}] {note_content}"
                    if '비고' in col_idx_map:
                        sheet.update_cell(idx, col_idx_map['비고'], new_note)
            return jsonify({'status': 'success', 'message': '비고가 등록되었습니다.'})

        elif change_type == 'request_reschedule':
            req_date = reschedule_data.get('date')
            req_time = reschedule_data.get('time')
            req_reason = reschedule_data.get('reason', '')

            target_row_num = find_target_row()
            if target_row_num:
                current_note = str(records[target_row_num - 2].get('비고', '')).strip()
                if '상태' in col_idx_map:
                    sheet.update_cell(target_row_num, col_idx_map['상태'], '일정변경요청')
                if '비고' in col_idx_map:
                    new_note = f"[일정변경요청] 희망일시: {req_date} {req_time} / 사유: {req_reason}"
                    if current_note:
                        new_note += f" ({current_note})"
                    sheet.update_cell(target_row_num, col_idx_map['비고'], new_note)
                return jsonify({'status': 'success', 'message': '해당 엔지니어의 일정 변경 요청이 접수되었습니다.'})
            else:
                return jsonify({'status': 'error', 'message': '일정 변경을 요청할 엔지니어를 찾지 못했습니다.'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'처리 중 오류 발생: {str(e)}'})

# ==================== [2] 관리자용 라우트 (/admin) ====================

@app.route('/admin')
def admin_page():
    if not session.get('admin_logged_in'):
        return redirect(url_for('index'))

    now = datetime.now()
    current_m = f"{now.month}월"
    next_m = f"{(now + relativedelta(months=1)).month}월"
    return render_template('admin.html', current_month=current_m, next_month=next_m)

@app.route('/admin/get-schedules', methods=['POST'])
def admin_get_schedules():
    data = request.get_json()
    target_month = data.get('target_month', '').strip()
    try:
        sheet = get_google_sheet(target_month)
        records = sheet.get_all_records()
        
        formatted_records = []
        for idx, raw_row in enumerate(records, start=2):
            row = {str(k).strip(): v for k, v in raw_row.items()}
            dec_birth = decrypt_pii(row.get('생년월일', ''))
            dec_phone = decrypt_pii(row.get('연락처', ''))
            dec_email = decrypt_pii(row.get('이메일', ''))

            formatted_records.append({
                "row_index": idx,
                "업체명": str(row.get('업체명', '')).strip(),
                "이름": str(row.get('이름', '')).strip(),
                "생년월일": format_birthdate(dec_birth),
                "이메일": dec_email,
                "연락처": format_phone(dec_phone),
                "점검일자": str(row.get('점검일자', '')).strip(),
                "점검시간": str(row.get('점검시간', '')).strip(),
                "상태": str(row.get('상태', '')).strip(),
                "비고": str(row.get('비고', '')).strip()
            })
        return jsonify({"status": "success", "schedules": formatted_records})
    except Exception as e:
        return jsonify({"status": "error", "message": f"시트를 불러오는 중 오류 발생: {str(e)}"})

@app.route('/admin/process-reschedule', methods=['POST'])
def admin_process_reschedule():
    data = request.get_json()
    target_month = data.get('target_month')
    row_index = data.get('row_index')
    company_name = data.get('company_name')
    action = data.get('action') 
    
    try:
        sheet = get_google_sheet(target_month)
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
        col_idx_map = {col_name.strip(): i + 1 for i, col_name in enumerate(headers)}
        
        for idx, row in enumerate(records, start=2):
            is_target = (row_index and idx == int(row_index)) or (not row_index and str(row.get('업체명', '')).strip() == company_name)
            if is_target:
                if action == 'approve':
                    note = str(row.get('비고', ''))
                    if "희망일시:" in note:
                        try:
                            part = note.split("희망일시:")[1].split("/")[0].strip()
                            req_date, req_time = part.split(" ")
                            if '점검일자' in col_idx_map:
                                sheet.update_cell(idx, col_idx_map['점검일자'], req_date)
                            if '점검시간' in col_idx_map:
                                sheet.update_cell(idx, col_idx_map['점검시간'], req_time)
                        except Exception:
                            pass
                    
                    if '상태' in col_idx_map:
                        sheet.update_cell(idx, col_idx_map['상태'], '일정변경완료')
                elif action == 'reject':
                    if '상태' in col_idx_map:
                        sheet.update_cell(idx, col_idx_map['상태'], '확인대기')
                        
        return jsonify({"status": "success", "message": "처리가 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"처리 중 오류 발생: {str(e)}"})

@app.route('/admin/edit-schedule', methods=['POST'])
def admin_edit_schedule():
    data = request.get_json()
    target_month = data.get('target_month')
    row_index = data.get('row_index')
    updated_data = data.get('updated_data', {})

    try:
        sheet = get_google_sheet(target_month)
        headers = sheet.row_values(1)
        col_idx_map = {col_name.strip(): i + 1 for i, col_name in enumerate(headers)}

        if '업체명' in col_idx_map and '업체명' in updated_data: sheet.update_cell(row_index, col_idx_map['업체명'], updated_data['업체명'])
        if '이름' in col_idx_map and '이름' in updated_data: sheet.update_cell(row_index, col_idx_map['이름'], updated_data['이름'])
        if '이메일' in col_idx_map and '이메일' in updated_data: sheet.update_cell(row_index, col_idx_map['이메일'], encrypt_pii(updated_data['이메일']))
        if '점검일자' in col_idx_map and '점검일자' in updated_data: sheet.update_cell(row_index, col_idx_map['점검일자'], updated_data['점검일자'])
        if '점검시간' in col_idx_map and '점검시간' in updated_data: sheet.update_cell(row_index, col_idx_map['점검시간'], updated_data['점검시간'])
        if '상태' in col_idx_map and '상태' in updated_data: sheet.update_cell(row_index, col_idx_map['상태'], updated_data['상태'])
        if '비고' in col_idx_map and '비고' in updated_data: sheet.update_cell(row_index, col_idx_map['비고'], updated_data['비고'])

        return jsonify({"status": "success", "message": "데이터가 성공적으로 수정되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"수정 중 오류 발생: {str(e)}"})

@app.route('/admin/delete-schedules', methods=['POST'])
def admin_delete_schedules():
    data = request.get_json()
    target_month = str(data.get('target_month', '')).strip()
    row_indices = data.get('row_indices', [])

    if not target_month or not row_indices:
        return jsonify({"status": "error", "message": "삭제할 일정을 선택해주세요."})

    try:
        sheet = get_google_sheet(target_month)

        valid_indices = sorted({int(idx) for idx in row_indices if int(idx) >= 2}, reverse=True)
        if not valid_indices:
            return jsonify({"status": "error", "message": "유효한 삭제 대상 행이 없습니다."})

        ranges = []
        end_idx = start_idx = valid_indices[0]
        for idx in valid_indices[1:]:
            if idx == start_idx - 1:
                start_idx = idx
            else:
                ranges.append((start_idx, end_idx))
                end_idx = start_idx = idx
        ranges.append((start_idx, end_idx))

        for s_idx, e_idx in ranges:
            if s_idx == e_idx:
                sheet.delete_rows(s_idx)
            else:
                sheet.delete_rows(s_idx, e_idx)

        return jsonify({
            "status": "success",
            "message": f"선택한 {len(valid_indices)}건의 일정이 삭제되었습니다."
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"일괄 삭제 중 오류 발생: {str(e)}"})
        
# ==================== [3] 일정 일괄 등록 라우트 (중복 방지 덮어쓰기 지원) ====================

@app.route('/admin/save-ocr-batch', methods=['POST'])
def admin_save_ocr_batch():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "요청 데이터가 없습니다."})

    default_month = str(data.get('target_month', '')).strip()
    rows = data.get('rows', [])
    overwrite_confirmed = bool(data.get('overwrite_confirmed', False))

    if not rows:
        return jsonify({"status": "error", "message": "저장할 데이터가 없습니다."})

    try:
        grouped_rows = {}
        for item in rows:
            check_date = str(item.get('점검일자', '')).strip()
            m = re.match(r'^\d{4}-(\d{1,2})-\d{1,2}$', check_date)
            actual_month = f"{int(m.group(1))}월" if m else default_month

            if not actual_month:
                continue
            grouped_rows.setdefault(actual_month, []).append(item)

        # 1단계: 대상 월 시트에 기존 데이터가 있는지 사전 확인
        existing_info = {}
        for sheet_month in grouped_rows.keys():
            sheet = get_google_sheet(sheet_month)
            existing_records = sheet.get_all_records()
            if len(existing_records) > 0:
                existing_info[sheet_month] = len(existing_records)

        if existing_info and not overwrite_confirmed:
            detail_str = ", ".join([f"[{m}] {cnt}건" for m, cnt in existing_info.items()])
            return jsonify({
                "status": "confirm_overwrite",
                "message": f"이미 등록된 데이터가 존재합니다 ({detail_str}).\n기존 데이터를 모두 삭제한 뒤 새 일정으로 일괄 등록하시겠습니까?"
            })

        # 2단계: 확인 완료 시 기존 데이터 일괄 삭제 후 신규 데이터 삽입
        saved_summary = []
        default_headers = ["업체명", "이름", "성별", "연락처", "생년월일", "이메일", "점검일자", "점검시간", "상태", "비고"]

        for sheet_month, month_items in grouped_rows.items():
            sheet = get_google_sheet(sheet_month)
            headers = sheet.row_values(1)

            if not headers:
                headers = default_headers
                sheet.append_row(headers)

            # 기존 데이터 행(2행 ~ 마지막 행) 일괄 삭제
            total_rows = len(sheet.get_all_values())
            if total_rows >= 2:
                sheet.delete_rows(2, total_rows)

            col_idx_map = {col_name.strip(): i + 1 for i, col_name in enumerate(headers)}
            rows_to_append = []

            for item in month_items:
                new_row = [''] * len(headers)
                if '업체명' in col_idx_map:
                    new_row[col_idx_map['업체명'] - 1] = normalize_company_name(item.get('업체명', ''))
                if '이름' in col_idx_map:
                    new_row[col_idx_map['이름'] - 1] = str(item.get('이름', '')).strip()
                if '점검일자' in col_idx_map:
                    new_row[col_idx_map['점검일자'] - 1] = str(item.get('점검일자', '')).strip()
                if '점검시간' in col_idx_map:
                    new_row[col_idx_map['점검시간'] - 1] = str(item.get('점검시간', '')).strip()
                if '상태' in col_idx_map:
                    new_row[col_idx_map['상태'] - 1] = '확인대기'
                if '비고' in col_idx_map:
                    new_row[col_idx_map['비고'] - 1] = ''

                rows_to_append.append(new_row)

            if rows_to_append:
                sheet.append_rows(rows_to_append, value_input_option='USER_ENTERED')
                saved_summary.append(f"[{sheet_month}] {len(rows_to_append)}건")

        return jsonify({
            "status": "success",
            "message": f"✅ 일괄 등록 완료: {', '.join(saved_summary)}"
        })

    except Exception as e:
        return jsonify({"status": "error", "message": f"시트 저장 중 오류 발생: {str(e)}"})

# ==================== [4] 업체 관리 라우트 ====================

@app.route('/admin/get-companies', methods=['GET'])
def admin_get_companies():
    try:
        sheet = get_google_sheet("업체")
        records = sheet.get_all_records()
        formatted = []
        for idx, raw_row in enumerate(records, start=2):
            row = {str(k).strip(): v for k, v in raw_row.items()}
            raw_pin = str(row.get('PIN', row.get('비밀번호', ''))).strip()
            formatted.append({
                "row_index": idx,
                "업체명": str(row.get('업체명', '')).strip(),
                "PIN": decrypt_pii(raw_pin),
                "점검시각": str(row.get('점검시각', '')).strip(),
                "비고": str(row.get('비고', '')).strip()
            })
        return jsonify({"status": "success", "companies": formatted})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/admin/edit-company', methods=['POST'])
def admin_edit_company():
    data = request.get_json()
    row_index = data.get('row_index')
    updated_data = data.get('updated_data', {})
    new_pin = str(updated_data.get('PIN', '')).strip()
    
    try: 
        sheet = get_google_sheet("업체")
        records = sheet.get_all_records()
        headers = sheet.row_values(1)
        col_idx_map = {col_name.strip(): i + 1 for i, col_name in enumerate(headers)}

        for idx, raw_row in enumerate(records, start=2):
            row = {str(k).strip(): v for k, v in raw_row.items()}
            if idx != row_index:
                existing_pin = decrypt_pii(str(row.get('PIN', row.get('비밀번호', ''))).strip())
                if new_pin and existing_pin == new_pin:
                    return jsonify({"status": "error", "message": f"이미 다른 업체에서 사용 중인 PIN 번호('{new_pin}')입니다."})

        if '업체명' in col_idx_map:
            sheet.update_cell(row_index, col_idx_map['업체명'], updated_data.get('업체명', ''))
        
        enc_pin = encrypt_pii(new_pin)
        if 'PIN' in col_idx_map:
            sheet.update_cell(row_index, col_idx_map['PIN'], enc_pin)
        elif '비밀번호' in col_idx_map:
            sheet.update_cell(row_index, col_idx_map['비밀번호'], enc_pin)
            
        if '점검시각' in col_idx_map:
            sheet.update_cell(row_index, col_idx_map['점검시각'], updated_data.get('점검시각', ''))
        if '비고' in col_idx_map:
            sheet.update_cell(row_index, col_idx_map['비고'], updated_data.get('비고', ''))

        return jsonify({"status": "success", "message": "업체 정보가 성공적으로 수정되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/admin/add-company', methods=['POST'])
def admin_add_company():
    data = request.get_json()
    company_name = str(data.get('업체명', '')).strip()
    pin = str(data.get('PIN', '')).strip()
    fixed_time = str(data.get('점검시각', '')).strip()
    note = str(data.get('비고', '')).strip()

    if not company_name or not pin:
        return jsonify({"status": "error", "message": "업체명과 PIN 번호는 필수 입력 항목입니다."})

    try:
        sheet = get_google_sheet("업체")
        records = sheet.get_all_records()
        headers = sheet.row_values(1)

        if not headers:
            headers = ["업체명", "PIN", "점검시각", "비고"]
            sheet.append_row(headers)

        for raw_row in records:
            row = {str(k).strip(): v for k, v in raw_row.items()}
            existing_comp = str(row.get('업체명', '')).strip()
            existing_pin = decrypt_pii(str(row.get('PIN', row.get('비밀번호', ''))).strip())
            if existing_comp == company_name:
                return jsonify({"status": "error", "message": f"이미 등록된 업체명('{company_name}')입니다."})
            if existing_pin == pin:
                return jsonify({"status": "error", "message": f"이미 다른 업체에서 사용 중인 PIN 번호('{pin}')입니다."})

        col_idx_map = {col_name.strip(): i + 1 for i, col_name in enumerate(headers)}
        new_row = [''] * len(headers)

        enc_pin = encrypt_pii(pin)
        if '업체명' in col_idx_map:
            new_row[col_idx_map['업체명'] - 1] = company_name
        if 'PIN' in col_idx_map:
            new_row[col_idx_map['PIN'] - 1] = enc_pin
        elif '비밀번호' in col_idx_map:
            new_row[col_idx_map['비밀번호'] - 1] = enc_pin
        if '점검시각' in col_idx_map:
            new_row[col_idx_map['점검시각'] - 1] = fixed_time
        if '비고' in col_idx_map:
            new_row[col_idx_map['비고'] - 1] = note

        sheet.append_row(new_row)
        return jsonify({"status": "success", "message": f"[{company_name}] 업체가 신규 등록되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"업체 추가 중 오류 발생: {str(e)}"})

@app.route('/admin/encrypt-existing-pins', methods=['GET'])
def admin_encrypt_existing_pins():
    try:
        sheet = get_google_sheet("업체")
        headers = [h.strip() for h in sheet.row_values(1)]
        records = sheet.get_all_records()

        pin_col_idx = None
        if 'PIN' in headers:
            pin_col_idx = headers.index('PIN') + 1
        elif '비밀번호' in headers:
            pin_col_idx = headers.index('비밀번호') + 1

        if not pin_col_idx:
            return jsonify({"status": "error", "message": "시트에서 PIN 컬럼을 찾을 수 없습니다."})

        cells_to_update = []
        for idx, raw_row in enumerate(records, start=2):
            row = {str(k).strip(): v for k, v in raw_row.items()}
            raw_pin = str(row.get('PIN', row.get('비밀번호', ''))).strip()
            if raw_pin and not raw_pin.startswith("gAAAAAB"):
                cells_to_update.append(gspread.Cell(idx, pin_col_idx, encrypt_pii(raw_pin)))

        if cells_to_update:
            sheet.update_cells(cells_to_update)

        return jsonify({
            "status": "success",
            "message": f"기존 평문 PIN {len(cells_to_update)}건 일괄 암호화 완료!"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"일괄 암호화 중 오류: {str(e)}"})

if __name__ == '__main__':
    app.run(debug=True)
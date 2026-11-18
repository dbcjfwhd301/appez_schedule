import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. 구글 시트 API 인증 세팅
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)

try:
    # 2. 구글 시트 열기 
    doc = client.open("automation")
    sheet = doc.get_worksheet(0) # 첫 번째 탭 선택
    
    # A1 셀에 텍스트 쓰기
    sheet.update_acell('A1', 'Success!')
    print("Ok!")
    
except Exception as e:
    print(f"error: {e}")
import os
import json
from pathlib import Path
from cryptography.fernet import Fernet

def get_config_dir():
    if os.name == 'nt':
        appdata = os.getenv('APPDATA')
        config_dir = Path(appdata) / 'HoseoMacro'
    else:
        config_dir = Path.home() / '.hoseo_macro'
    
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir

def get_config_path():
    return get_config_dir() / 'config.json'

def get_key_path():
    return get_config_dir() / '.key'

def get_or_create_key():
    key_path = get_key_path()
    
    if key_path.exists():
        with open(key_path, 'rb') as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(key_path, 'wb') as f:
            f.write(key)
        if os.name == 'nt':
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(key_path), 2)
            except:
                pass
    
    return key

def encrypt_password(password):
    if not password:
        return ""
    
    key = get_or_create_key()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(password.encode())
    return encrypted.decode()

def decrypt_password(encrypted_password):
    if not encrypted_password:
        return ""
    
    try:
        key = get_or_create_key()
        fernet = Fernet(key)
        decrypted = fernet.decrypt(encrypted_password.encode())
        return decrypted.decode()
    except Exception as e:
        print(f"복호화 오류: {e}")
        return ""

def save_config(user_id, password, remember_me, selected_courses=None):
    current_config = {}
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                current_config = json.load(f)
        except:
            pass

    if selected_courses is None:
        selected_courses = current_config.get("selected_courses", [])

    config = {
        "remember_me": remember_me,
        "user_id_encrypted": encrypt_password(user_id) if remember_me else "",
        "password_encrypted": encrypt_password(password) if remember_me else "",
        "selected_courses": selected_courses
    }
    
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"설정 저장 오류: {e}")
        return False

def load_config():
    config_path = get_config_path()
    
    if not config_path.exists():
        return {
            "remember_me": False,
            "user_id": "",
            "password": "",
            "selected_courses": None
        }
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        encrypted_id = config.get("user_id_encrypted", "")
        encrypted_pw = config.get("password_encrypted", "")
        
        config["user_id"] = decrypt_password(encrypted_id)
        config["password"] = decrypt_password(encrypted_pw)
        
        config.setdefault("remember_me", False)
        if "selected_courses" not in config:
            config["selected_courses"] = None
        
        return config
    except Exception as e:
        print(f"설정 로드 오류: {e}")
        return {
            "remember_me": False,
            "user_id": "",
            "password": "",
            "selected_courses": None
        }

if __name__ == "__main__":
    print(f"설정 디렉토리: {get_config_dir()}")
    print(f"설정 파일 경로: {get_config_path()}")
    
    save_config("test_user", "test_password_123", True, ["강의1", "강의2"])
    print("설정 저장 완료")
    
    loaded = load_config()
    print(f"로드된 설정: {loaded}")
    print(f"비밀번호 복호화 성공: {loaded['password'] == 'test_password_123'}")

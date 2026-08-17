from flask import Flask
from flask_cors import CORS

from etf_classifier import etf_bp

app = Flask(__name__)

# 워드프레스(다른 도메인)에서 fetch로 이 API를 부르니까 CORS를 허용해야 함.
# 필요하면 origins=["https://내블로그도메인.com"] 처럼 좁혀도 됨.
CORS(app)

app.register_blueprint(etf_bp)

if __name__ == "__main__":
    app.run(debug=True)

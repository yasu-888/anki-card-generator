import os
import uuid
import json
import base64
import asyncio
import functions_framework
from flask import jsonify
from google import genai
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Tuple
import textwrap
from gtts import gTTS
from io import BytesIO
from datetime import datetime
from urllib.parse import quote
from distutils.util import strtobool
import requests
import threading
from concurrent.futures import ThreadPoolExecutor

# HTTP Functions向けのカスタム例外
class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(self.detail)

# GeminiのアプトプットのJSONスキーマ定義
class WordAnalysis(BaseModel):
    contextual_translation: str = Field(description="[日本語必須] この場面での文章の意訳（日本語の感覚でどのような意味か、ニュアンスを大切に）")
    precise_translation: str = Field(description="[日本語必須] 単語の意味を正確に捉えた、文章の正確な日本語訳")
    frequency_rating: int = Field(description="単語の日常会話での出現頻度、英会話学習における単語の重要度を点数化", ge=1, le=5)
    ipa: str = Field(description="単語の発音記号（アメリカ英語）")
    part_of_speech: str = Field(description="[ENGLISH ONLY] 文章中での品詞（noun, verb, adjective, adverb, etc.）")
    english_definition: str = Field(description="[ENGLISH ONLY] できるだけ最小限に短く、中学・高校英語レベルでの簡潔な定義")
    japanese_meaning: str = Field(description="[日本語必須] この特定の文脈における単語の最も適切な日本語訳と簡潔な説明")
    example_sentence: str = Field(description="[英語+日本語] 単語を使った今日から使える簡単な例文「英語文（日本語訳）」形式")
    core_meaning: Optional[str] = Field(description="[日本語必須] 単語の核となる意味や語源的説明。別の文脈での意味も含む")
    antonyms: List[str] = Field(description="[ENGLISH ONLY] 中学・高校英語レベルでの対義語のリスト（重要度順）")
    synonyms: List[str] = Field(description="[ENGLISH ONLY] 中学・高校英語レベルでの類義語のリスト（最も近い意味順）") 
    slang: Optional[str] = Field(description="[英語+日本語] 文中に含まれるスラング表現の説明")
    idioms: Optional[str] = Field(description="[英語+日本語] 文中に含まれる熟語・連語・群動詞・慣用句の説明。熟語の判定は緩くて問題ないです。特別な単語の組み合わせパターンがあれば解説してください。")
    japanese_usage: Optional[str] = Field(description="[日本語必須] 日本でカタカナ英語や商品名として馴染み深いものの説明")
    memory_aids: Optional[str] = Field(description="[日本語必須] 単語を効果的に覚えるためのコツや関連付け")
    terminology: Optional[str] = Field(description="[日本語必須] IT、プログラミング、マーケティングでの専門用語になっているかどうか")
    explanation: str = Field(description="[日本語必須] 英単語・英文の説明の最終説明＆総括。")

# シングルトンパターンでGemini APIクライアントを初期化
class GeminiClient:
    _instance = None
    _model = None
    _lock = threading.Lock()
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:  # 二重チェックロック
                    gemini_api_key = os.environ.get("GEMINI_API_KEY", "your-gemini-api-key-here")
                    cls._instance = genai.Client(api_key=gemini_api_key)
                    cls._model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        return cls._instance, cls._model

def generate_unique_file_name(word):
    return f"{word}-{uuid.uuid4().hex[:6]}"

# gTTSを使用した音声生成（インメモリ処理）
def generate_audio_clip(sentence, unique_file_name):
    audio_embed = f"![[{unique_file_name}.mp3]]"
    
    # インメモリでMP3を生成
    mp3_fp = BytesIO()
    tts = gTTS(sentence, lang='en')
    tts.write_to_fp(mp3_fp)
    mp3_fp.seek(0)
    
    # Base64エンコード
    audio_base64 = base64.b64encode(mp3_fp.read()).decode('utf-8')
    
    return audio_base64, audio_embed

def create_prompt(sentence, word, tag):
    prompt = f"""
        <role>
        あなたは英語と日本語のバイリンガルで、英語学習者にとって最高の説明者です。
        あなたの重要な役割は、指定された言語（日本語または英語）で各項目を回答することです。
        各フィールドの言語指定を絶対に守ってください。
        </role>

        <task>
        {f'『{tag}』を視聴していた時に登場した、' if tag != "Other" else ''}セリフ英文「{sentence}」中の単語「{word}」について詳細に分析し、次のJSON形式で回答してください。
        </task>

        <audience>
        20〜30代歳男性の日本人。高校英語レベルは80%くらいの理解度だが英語での会話はかなり辿々しい。
        イマージョンラーニング（3年目）で英語学習中。日本のアニメや海外ドラマを英語音声・字幕で視聴しながら学習中。
        プログラミング、ビジネス、テクノロジー関連の用語には比較的馴染みがある。
        </audience>

        <importance>
        教育的価値の高い説明を提供し、特に「{word}」の意味、使い方、ニュアンスを明確に伝えてください。
        説明は簡潔に、中学・高校レベルの理解しやすい言葉を使用してください。
        対象者のプログラミングやマーケティングの知識を活かした例や説明があれば、それも取り入れてください。
        </importance>

        <field_language_rules>
        各フィールドには厳格な言語指定があります：
        - [日本語必須]: このフィールドは必ず日本語のみで記入してください。英語は一切使用禁止です。
        - [ENGLISH ONLY]: このフィールドは必ず英語のみで記入してください。日本語は一切使用禁止です。
        - [英語+日本語]: このフィールドは指定された形式（「英語文（日本語訳）」など）に厳密に従ってください。

        言語指定違反は回答全体の品質を著しく低下させるため、最優先事項として扱ってください。
        </field_language_rules>

        <field_guidelines>
        - contextual_translation: [日本語必須] この場面での文章の意訳（日本語の感覚でどのような意味か、ニュアンスを大切に）
        - precise_translation: [日本語必須] 単語「{word}」の意味を正確に捉えた、文章の正確な日本語訳
        - frequency_rating: [数字] 1(ほとんど使われない)〜5(非常によく使われる)の整数で表す。単語「{word}」の日常会話、海外ドラマ、アニメでの使用頻度、英会話学習における単語の重要度を点数化。
        - ipa: [IPA] 単語「{word}」のアメリカ英語発音記号
        - part_of_speech: [ENGLISH ONLY] 文章中での品詞（noun, verb, adjective, adverb, etc.）
        - english_definition: [ENGLISH ONLY] 30語以内の中学・高校英語レベルでの簡潔な英英定義
        - japanese_meaning: [日本語必須] この特定の文脈における単語の最も適切な日本語訳と簡潔な説明
        - example_sentence: [英語+日本語] 「{word}」を使った簡単な例文。「英語文（日本語訳）」形式厳守
        - core_meaning: [日本語必須] 単語の核となる意味や語源的説明、別の文脈での意味も含む
        - antonyms: [ENGLISH ONLY] 中学・高校英語レベルでの対義語リスト（重要度順）。（できるだけ中学レベルの単語で）
        - synonyms: [ENGLISH ONLY] 中学・高校英語レベルでの類義語リスト（最も近い意味順）。（できるだけ中学レベルの単語で）
        - slang: [英語+日本語] 文中に含まれるスラング表現の説明。「英語（日本語説明）」形式
        - idioms: [英語+日本語] 文中に含まれる文中に含まれる熟語・連語・群動詞・慣用句の説明。熟語の判定は緩くて問題ないです。特別な単語の組み合わせパターンがあれば解説してください。「英語（日本語説明）」形式
        - japanese_usage: [日本語必須] 日本でのカタカナ英語や商品名としての馴染み
        - memory_aids: [日本語必須] 単語を効果的に覚えるためのコツや関連付け方法
        - terminology: [日本語必須] IT、プログラミング、マーケティングでの専門用語としての使われ方
        - explanation: [日本語必須] 英単語・英文の最終説明と総括
        </field_guidelines>

        <empty_string_and_empty_array_rules>
        該当する情報がない場合は、以下のルールを厳守してください：
        - Optional[str]型フィールド: 空文字列""を使用
        - List[str]型フィールド: 空配列[]を使用
        これは出力JSONの処理のために非常に重要です。
        </empty_string_and_empty_array_rules>

        <language_validation>
        回答を提出する前に、以下を確認してください：
        1. [日本語必須] フィールドに英単語や英文が含まれていないか
        2. [ENGLISH ONLY] フィールドに日本語が含まれていないか
        3. [英語+日本語] フィールドが指定形式に従っているか
        4. 空値ルールが正しく適用されているか

        全ての言語指定を守ることで、英語学習者にとって最高の学習リソースを提供できます。
        </language_validation>

        <output_constraints>
        - すべてのフィールドは指定された要件を満たし、形式を厳守します。
        - 特に言語指定に関しては、絶対に違反しないでください：
        - [日本語必須] のフィールドには日本語のみを使用し、英語は一切含めないでください。
        - [ENGLISH ONLY] のフィールドには英語のみを使用し、日本語は一切含めないでください。
        - [英語+日本語] のフィールドは指定された形式（「英語文（日本語訳）」など）に厳密に従ってください。
        - 理解を助けるようなわかりやすい情報がない場合は、無理に項目を埋めようとせず、下記のルールに従って、回答なしの意思を示してください：
        - 文字列型のオプショナルフィールドは必ず空文字列""を使用
        - 配列型のフィールドで項目がない場合は必ず空配列[]を使用
        - 言語指定違反は回答全体の品質を著しく低下させるため、特に注意してください。
        </output_constraints>
        """
    return textwrap.dedent(prompt)

# Gemini APIを呼び出す関数
def analysis_words_by_gemini(prompt):
    gemini_client, gemini_model = GeminiClient.get_instance()
    
    response = gemini_client.models.generate_content(
        model=gemini_model,
        contents=prompt,
        config={
            'response_mime_type': 'application/json',
            'response_schema': WordAnalysis,
        },
    )
    
    response_parsed: WordAnalysis = response.parsed
    response_dict_str = response_parsed.model_dump_json(indent=2)
    return json.loads(response_dict_str)

# データのフォーマット
def create_formatted_data(sentence, word, unique_file_name, response_dict, ex_audio_base64, ex_audio_embed):

    created_at = datetime.now().strftime('%Y-%m-%d %H:%M')
    job_id = str(uuid.uuid4())
    obsidian_uri = f"obsidian://open?vault=anki-vault&file=AnkiCard%2F{quote(word)}"
    playphrase_me_url = f"https://www.playphrase.me/#/search?q={quote(word)}"
    image_embed = f"![[{unique_file_name}.jpeg]]"
    highlighted_sentence = sentence.replace(word, f"=={word}==")
    
    # レーティングを星に
    rating_num = response_dict["frequency_rating"]
    rating_star = "★" * rating_num

    # ターゲットデッキ決定
    if rating_num in [5, "5"]:
        target_deck = "Immersion::01-Frequent"
    elif rating_num in [4, 3, "4", "3"]:
        target_deck = "Immersion::02-Common"
    elif rating_num in [2, 1, "2", "1"]:
        target_deck = "Immersion::03-Rare"
    else:
        target_deck = "Immersion"
    
    # 例文内の単語をBold 全体をItalic
    example_sentence = response_dict["example_sentence"]
    
    if "（" in example_sentence and "）" in example_sentence:
        ex_sentence_english, ex_sentence_japanese = example_sentence.split("（", 1)
        ex_sentence_japanese = ex_sentence_japanese.replace("）", "")
    else:
        ex_sentence_english = example_sentence
        ex_sentence_japanese = ""
    
    formatted_ex_english = f"*{ex_sentence_english.replace(word, f'**{word}**')}*"
    formatted_ex_sentence = f"*{ex_sentence_english.replace(word, f'**{word}**')}* ({ex_sentence_japanese})"
    formatted_ex_english_audio = f"*{ex_sentence_english.replace(word, f'**{word}**')}* {ex_audio_embed}"
    
    # 類義語、対義語
    synonyms_str = ", ".join(response_dict["synonyms"]) if response_dict["synonyms"] else ""
    antonyms_str = ", ".join(response_dict["antonyms"]) if response_dict["antonyms"] else ""
    
    # 結果辞書
    result = {
        "created_at": created_at,
        "job_id": job_id,
        "obsidian_uri": obsidian_uri,
        "playphrase_me_url": playphrase_me_url,
        "image_embed": image_embed,
        "highlighted_sentence": highlighted_sentence,
        "rating_star": rating_star,
        "target_deck": target_deck,
        "formatted_ex_english": formatted_ex_english,
        "ex_sentence_english": ex_sentence_english,
        "ex_sentence_japanese": ex_sentence_japanese,
        "formatted_ex_sentence": formatted_ex_sentence,
        "formatted_ex_english_audio": formatted_ex_english_audio,
        "ex_audio_base64": ex_audio_base64,
        "ex_audio_embed": ex_audio_embed,
        "synonyms_str": synonyms_str,
        "antonyms_str": antonyms_str,
    }
    
    # response_dictの内容をマージ
    result.update(response_dict)
    
    return result

def create_anki_template(json_data, word, tag, audio_embed):
    anki_template = f"""
        TARGET DECK: {json_data.get("target_deck", "Immersion")}
        START
        Immersion
        Image: {json_data.get("image_embed", None)}
        Sentence: {json_data.get("highlighted_sentence", None)}
        NaturalJapanese: {json_data.get("contextual_translation", None)}
        Japanese: {json_data.get("precise_translation", None)}
        Word: {word}
        IPA: {json_data.get("ipa", None)}
        PartOfSpeech: {json_data.get("part_of_speech", None)}
        Definition: {json_data.get("english_definition", None)}
        Synonyms: {json_data.get("synonyms_str", None)}
        Antonyms: {json_data.get("antonyms_str", None)}
        JapaneseMeaning: {json_data.get("japanese_meaning", None)}
        ExampleSentence: {json_data.get("ex_sentence_english", None)}
        ExSentenceJapanese: {json_data.get("ex_sentence_japanese", None)}
        Core: {json_data.get("core_meaning", None)}
        MemoryAids: {json_data.get("memory_aids", None)}
        JapaneseUsage: {json_data.get("japanese_usage", None)}
        Terminology: {json_data.get("terminology", None)}
        Idioms: {json_data.get("idioms", None)}
        Slang: {json_data.get("slang", None)}
        Rating: {json_data.get("rating_star", None)}
        Explanation: {json_data.get("explanation", None)}
        Voice: {audio_embed}
        ObsidianLink: {json_data.get("obsidian_uri", None)}
        PlayPhraseMe: {json_data.get("playphrase_me_url", None)}
        Tags: {tag}
        END
        """
    
    return textwrap.dedent(anki_template)

# Notionへの保存（非同期処理）
def save_to_notion(json_data, sentence, word, tag):
    NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
    NOTION_DB_ID = os.environ.get("NOTION_DB_ID")

    # 環境変数の必須チェック
    if not NOTION_TOKEN or not NOTION_DB_ID:
        print("Warning: NOTION_TOKEN or NOTION_DB_ID not set. Skipping Notion save.")
        return None
    
    url = "https://api.notion.com/v1/pages/"
    headers = {
        'Content-Type': 'application/json',
        'Notion-Version': '2022-02-22',
        'Authorization': f'Bearer {NOTION_TOKEN}',
    }
    
    # ペイロードの構築
    payload = {
        "parent": {
            "database_id": NOTION_DB_ID
        },
        "properties": {
            "Sentence": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": sentence
                    }
                }]
            },
            "Word": {
                "title": [{
                    "type": "text",
                    "text": {
                        "content": word
                    }
                }]
            },
            "Tags": {
                "multi_select": [{
                    "name": tag
                }]
            },
            "NaturalJapanese": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('contextual_translation', '')
                    }
                }]
            },
            "Japanese": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('precise_translation', '')
                    }
                }]
            },
            "Idioms": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('idioms', '')
                    }
                }]
            },
            "Slang": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('slang', '')
                    }
                }]
            },
            "RatingNum": {
                "number": json_data.get('frequency_rating', None)
            },
            "Rating": {
                "select": {
                    "name": json_data.get('rating_star', '')
                }
            },
            "Definition": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('english_definition', '')
                    }
                }]
            },
            "JapaneseMeaning": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('japanese_meaning', '')
                    }
                }]
            },
            "IPA": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('ipa', '')
                    }
                }]
            },
            "PartOfSpeech": {
                "select": {
                    "name": json_data.get('part_of_speech', '')
                }
            },
            "ExampleSentence": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('ex_sentence_english', '') + '( ' + json_data.get('ex_sentence_japanese', '') + ')'
                    }
                }]
            },
            "≈ synonyms": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('synonyms_str', '')
                    }
                }]
            },
            "↔︎ antonyms": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('antonyms_str', '')
                    }
                }]
            },
            "Core": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('core_meaning', '')
                    }
                }]
            },
            "MemoryAids": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('memory_aids', '')
                    }
                }]
            },
            "JapaneseUsage": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('japanese_usage', '')
                    }
                }]
            },
            "Terminology": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('terminology', '')
                    }
                }]
            },
            "Obsidian": {
                "url": json_data.get('obsidian_uri', None)
            },
            "Movie": {
                "url": json_data.get('playphrase_me_url', None)
            },
            "AnkiDeck": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('target_deck', '')
                    }
                }]
            },
            "id": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": json_data.get('job_id', '')
                    }
                }]
            }
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            print(f"Notion API error: {response.text}")
        return response.json() if response.status_code == 200 else None
    except Exception as e:
        print(f"Notion API request error: {str(e)}")
        return None

# スレッドプール実行器
executor = ThreadPoolExecutor(max_workers=10)

@functions_framework.http
def main_function(request):
    try:
        # リクエストのチェック
        if not request.is_json:
            return jsonify({'error': 'Unsupported Media Type'}), 415
        
        request_dict = request.get_json()
        
        # 必須フィールドの確認
        if not all(key in request_dict for key in ['sentence', 'word', 'tag']):
            return jsonify({'error': 'Missing required fields'}), 400
        
        sentence = request_dict['sentence']
        word = request_dict['word']
        tag = request_dict['tag']
        
        # ユニークなファイル名の生成
        unique_file_name = generate_unique_file_name(word)
        
        # プロンプトの作成
        prompt = create_prompt(sentence, word, tag)
        
        # ThreadPoolExecutorを使用した並列処理
        with ThreadPoolExecutor() as executor:
            # API呼び出しと音声生成を並列実行
            future_gemini = executor.submit(analysis_words_by_gemini, prompt)
            future_audio = executor.submit(generate_audio_clip, sentence, unique_file_name)
            
            # 結果の取得
            response_dict = future_gemini.result()
            audio_base64, audio_embed = future_audio.result()
            
            # 例文を抽出
            example_sentence = response_dict.get("example_sentence", "")
            if "（" in example_sentence and "）" in example_sentence:
                ex_sentence_english = example_sentence.split("（", 1)[0]
            else:
                ex_sentence_english = example_sentence
                
            # 例文音声を生成
            future_ex_audio = executor.submit(generate_audio_clip, ex_sentence_english, f"{unique_file_name}_example")
            ex_audio_base64, ex_audio_embed = future_ex_audio.result()
        
        # データフォーマット
        formatted_data = create_formatted_data(sentence, word, unique_file_name, response_dict, ex_audio_base64, ex_audio_embed)
        
        # Ankiテンプレート作成
        anki_template = create_anki_template(formatted_data, word, tag, audio_embed)

        # USE_NOTION環境変数でNotionへの保存を制御
        use_notion = bool(strtobool(os.environ.get("USE_NOTION", "false")))
        if use_notion:
            # Notionへの保存を別スレッドで実行（バックグラウンド処理）
            threading.Thread(
                target=save_to_notion,
                args=(formatted_data, sentence, word, tag),
                daemon=True
            ).start()
        
        # 最終的なレスポンスデータの準備
        result_dict = formatted_data.copy()
        result_dict["sentence"] = sentence
        result_dict["word"] = word
        result_dict['tag'] = tag
        result_dict["unique_file_name"] = unique_file_name
        result_dict["audio_base64"] = audio_base64
        result_dict["audio_embed"] = audio_embed
        result_dict["anki_template"] = anki_template
        
        # レスポンス返却
        return jsonify(result_dict), 200
        
    except HTTPException as e:
        return jsonify({'error': e.detail}), e.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500
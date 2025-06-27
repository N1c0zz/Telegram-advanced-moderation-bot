import logging
import os
import re
import functools
from typing import List, Dict, Any, Optional, Tuple

import unidecode
from openai import OpenAI, OpenAIError

from .config_manager import ConfigManager
from .cache_utils import MessageAnalysisCache
from .user_management import SystemPromptManager

try:
    import langdetect
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False
    logging.getLogger(__name__).warning("Libreria langdetect non trovata. Il rilevamento della lingua sarà limitato.")


class AdvancedModerationBotLogic:
    def __init__(self, config_manager: ConfigManager, logger: logging.Logger):
        self.config_manager = config_manager
        self.logger = logger
        self.banned_words: List[str] = self.config_manager.get('banned_words', [])
        self.whitelist_words: List[str] = self.config_manager.get('whitelist_words', [])
        self.allowed_languages: List[str] = self.config_manager.get('allowed_languages', ["italian"])
        self.logger.info(f"Whitelist caricata con {len(self.whitelist_words)} parole: {self.whitelist_words}")
        self.char_map: Dict[str, str] = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"}
        self.analysis_cache = MessageAnalysisCache(cache_size=1000)
        self.stats: Dict[str, Any] = {
            'total_messages_analyzed_by_openai': 0,
            'direct_filter_matches': 0,
            'ai_filter_violations': 0,
            'openai_requests': 0,
            'openai_cache_hits': 0,
        }
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            self.openai_client = OpenAI(api_key=api_key)
            self.logger.info("Client OpenAI inizializzato.")
        else:
            self.openai_client = None
            self.logger.warning("OPENAI_API_KEY non trovato. L'analisi AI non sarà disponibile.")       
        self.prompt_manager = SystemPromptManager(logger, self)

    @functools.lru_cache(maxsize=200)
    def contains_whitelist_word(self, text: str) -> bool:
        if not self.whitelist_words:
            return False
        normalized_text = self.normalize_text(text)
        if not normalized_text:
            return False
        for whitelist_word in self.whitelist_words:
            normalized_whitelist_word = whitelist_word.lower().strip()
            if normalized_whitelist_word in normalized_text:
                self.logger.debug(f"Whitelist match: '{whitelist_word}' trovata in '{text[:50]}...'")
                return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        total_analyzed = self.stats['total_messages_analyzed_by_openai']
        cache_hits = self.stats['openai_cache_hits']
        return {
            **self.stats,
            'cache_size': len(self.analysis_cache.cache),
            'cache_hit_rate': (cache_hits / total_analyzed) if total_analyzed > 0 else 0,
        }

    def normalize_text(self, text: str) -> str:
        text = re.sub(r'\*\*(.*?)\*\*', r'', text)
        text = re.sub(r'__(.*?)__', r'', text)
        text = re.sub(r'~~(.*?)~~', r'', text)
        text = re.sub(r'`(.*?)`', r'', text)
        text = re.sub(r'\[(.*?)\]\(.*?\)', r'', text)
        emoji_pattern = re.compile(
            "["
            u"😀-🙏"
            u"🌀-🗿"
            u"🚀-🛿"
            u"🇠-🇿"
            u"✂-➰"
            u"Ⓜ-🉑"
            u"☀-⛿"
            u"✀-➿"
            u"🔴🔵⚪⚫🟠🟡🟢🟣⚽⚾🥎🏀🏐🏈🏉🎱🪀🏓⚠️🚨🚫⛔️🆘🔔🔊📢📣"
            "]+", flags=re.UNICODE)
        text = emoji_pattern.sub('', text)
        text = unidecode.unidecode(text.lower())
        text = re.sub(r"[^a-z0-9\s@]", "", text)
        for char_from, char_to in self.char_map.items():
            text = text.replace(char_from, char_to)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @functools.lru_cache(maxsize=500)
    def contains_banned_word(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        self.logger.debug(f"Filtro diretto - Testo originale: '{text}'")
        text_lower = text.lower()
        for banned_word in self.banned_words:
            banned_word_lower = banned_word.lower().strip()
            if banned_word_lower in text_lower:
                self.logger.info(f"MATCH filtro diretto: parola bannata '{banned_word}' trovata in '{text[:50]}...'")
                return True
        telegram_link_patterns = [
            r'(?:https?://)?(?:t\.me|telegram\.me)/\w+',
            r'@\w+',
        ]
        material_offer_words = [
            'panieri', 'riassunti', 'appunti', 'materiale', 'slides', 'dispense',
            'tesi', 'esami', 'soluzioni', 'quiz', 'test', 'simulazioni'
        ]
        invitation_words = [
            'iscriversi', 'iscrivetevi', 'entrate', 'joinare', 'accedere', 'accesso',
            'canale', 'gruppo', 'link', 'qui', 'sotto', 'sopra', 'clicca', 'segui'
        ]
        has_telegram_link = any(re.search(pattern, text_lower, re.IGNORECASE) for pattern in telegram_link_patterns)
        has_material_offer = any(word in text_lower for word in material_offer_words)
        has_invitation = any(word in text_lower for word in invitation_words)
        if has_telegram_link and has_material_offer and has_invitation:
            self.logger.info(f"MATCH filtro diretto: link Telegram + offerta materiale + invito in '{text[:50]}...'")
            return True
        normalized_text = self.normalize_text(text)
        if not normalized_text:
            return False
        self.logger.debug(f"Filtro diretto - Testo normalizzato: '{normalized_text}'")
        cyrillic_count = sum(1 for char in text if 'Ѐ' <= char <= 'ӿ' or 'Ԁ' <= char <= 'ԯ')
        if cyrillic_count >= 3:
            self.logger.info(f"MATCH filtro diretto: {cyrillic_count} caratteri cirillici in '{text[:50]}...'")
            return True
        masked_panieri_spam_patterns = [
            r"chi\s+cerc[ao]\s+panier.*(?:scriv|contatt|privat)",
            r"cerc[ao]\s+panier.*(?:scriv|contatt|privat)",
            r"ho\s+(?:material|panier).*(?:scriv|contatt|privat)",
            r"(?:material|panier).*(?:complet|aggiornat).*(?:scriv|contatt|privat)",
            r"panier.*disponibil.*(?:scriv|contatt|privat|interessat)",
            r"panier.*(?:2024|2025|aggiornat).*(?:scriv|contatt|privat|interessat)",
            r"(?:scriv|contatt).*(?:per|sui)\s+panier",
            r"panier.*(?:scriv|contatt).*(?:privat|dm)",
            r"material.*(?:scriv|contatt).*(?:privat|dm)",
            r"interessat.*(?:scriv|contatt)",
            r"(?:scriv|contatt).*(?:per|chi)\s+(?:material|panier|appunt)",
            r"(?:material|panier).*(?:chi|per).*(?:scriv|contatt)",
            r"vendita.*(?:panier|riassunt|material).*(?:t\.me|telegram|canale)",
            r"(?:panier|riassunt|material).*vendita.*(?:t\.me|telegram|canale)",
            r"affidatevi.*(?:unico|solo).*canale.*(?:panier|riassunt|material)",
            r"canale.*(?:ufficiale|preposto).*(?:vendita|offerta).*(?:panier|riassunt)",
            r"@panieriunipegasomercatorum",
            r"@unitelematica",
        ]
        for pattern in masked_panieri_spam_patterns:
            if re.search(pattern, normalized_text, re.IGNORECASE):
                self.logger.info(f"MATCH filtro diretto (spam mascherato): pattern '{pattern}' in '{text}'")
                return True
        obvious_spam_patterns = [
            r"(?:vendo|offro).*[0-9]+\s*(?:euro|€).*(?:scriv|contatt|privat|whatsapp|telegram)",
            r"guadagni?\s+(?:facili|garantiti|sicuri)",
            r"(?:soldi|euro)\s+facili",
            r"mining.*pool.*(?:join|entra)",
            r"zarabotok",
            r"rabota",
            r"pishi",
            r"kontakt",
        ]
        for pattern in obvious_spam_patterns:
            if re.search(pattern, normalized_text, re.IGNORECASE):
                self.logger.info(f"MATCH filtro diretto: pattern '{pattern}' in '{normalized_text}'")
                return True
        return False

    def contains_suspicious_contact_invitation(self, text: str) -> bool:
        normalized_text = self.normalize_text(text)
        if not normalized_text: return False
        legitimate_contexts = [
            "grupp\w+\s+(?:studio|whatsapp|telegram)", "aggiung\w+\s+gruppo",
            "link\s+gruppo", "mandat\w+\s+numer\w+", "entrare\s+nel\s+gruppo"
        ]
        for legit_pattern in legitimate_contexts:
            if re.search(legit_pattern, normalized_text):
                if not any(term in normalized_text for term in ["vendo", "offro", "prezzo", "pagamento", "€", "euro"]):
                    self.logger.debug(f"Invito al contatto in contesto legittimo: '{normalized_text}'")
                    return False
        contact_channels = ["whatsapp", "telegram", "instagram", "dm", "direct", "privato", "@\w+"]
        contact_actions = ["scriv\w+", "contatt\w+", "mand\w+", "invia\w+", "messaggi\w+"]
        offered_items = [
            "panier\w+", "appunt\w+", "material\w+", "tesi", "esami", "soluzion\w+",
            "aiuto", "lezioni", "slides", "aggiornat\w+"
        ]
        has_contact_channel = any(re.search(channel, normalized_text) for channel in contact_channels)
        has_contact_action = any(action in normalized_text for action in contact_actions)
        has_offered_item = any(item in normalized_text for item in offered_items)
        if (has_contact_channel or has_contact_action) and has_offered_item:
            self.logger.debug(f"Rilevato invito al contatto sospetto: '{normalized_text}'")
            return True
        if re.search(r"@\w+", normalized_text) and has_offered_item:
            self.logger.debug(f"Rilevato @username con offerta materiale: '{normalized_text}'")
            return True
        return False
        
    def detect_language(self, text: str) -> Optional[str]:
        if not LANGDETECT_AVAILABLE or not text or len(text.strip()) < 5:
            return None 
        try:
            detected_langs = langdetect.detect_langs(text)
            if detected_langs:
                return detected_langs[0].lang
            return None
        except langdetect.lang_detect_exception.LangDetectException:
            self.logger.warning(f"Langdetect non è riuscito a rilevare la lingua per: '{text[:50]}...'")
            return None

    def is_language_disallowed(self, text: str) -> bool:
        if not self.allowed_languages or "any" in self.allowed_languages:
            return False
        clean_text = text.strip()
        if not clean_text:
            return False
        self.logger.debug(f"ℹ️ ANALISI LINGUA per: '{clean_text[:100]}...'")
        italian_indicators = {
            # Parole esistenti (mantenere)
            'ciao', 'buongiorno', 'buonasera', 'buonanotte', 'salve', 'arrivederci',
            'grazie', 'prego', 'scusa', 'scusate', 'perfetto', 'bene', 'male', 'così',
            'si', 'sì', 'no', 'ok', 'okay', 'va', 'sono', 'ho', 'hai', 'ha', 'abbiamo',
            'oggi', 'ieri', 'domani', 'quando', 'dove', 'come', 'cosa', 'chi', 'perché',
            'questo', 'quello', 'questi', 'quelli', 'che', 'con', 'per', 'del', 'della',
            'non', 'più', 'molto', 'poco', 'tutto', 'niente', 'anche', 'ancora', 'già',
            'usciti', 'uscito', 'uscita', 'esami', 'esame', 'paghi', 'finito',
            'appena', 'adesso', 'idem', 'conferma', 'parte', 'docente', 'luglio',
            'domande', 'domanda', 'paniere', 'panieri', 'sembra', 'entro', 'marzo', 'voti',
            'università', 'professore', 'prof', 'crediti', 'corso', 'corsi',
            'laurea', 'triennale', 'magistrale', 'dottorato', 'facoltà', 'appunti', 
            'lezioni', 'tesi', 'sessione', 'matricola', 'ateneo', 'dipartimento',
            'cattedra', 'semestre', 'frequenza', 'iscrizione', 'slides', 'slide',
            'boh', 'mah', 'beh', 'allora', 'quindi', 'però', 'infatti', 'comunque',
            'speriamo', 'magari', 'forse', 'davvero', 'veramente', 'sicuramente',
            'attendiamo', 'aspettiamo', 'vediamo', 'diciamo', 'facciamo', 'andiamo',
            'è', 'sono', 'siamo', 'sarà', 'sarebbe', 'potrebbe', 'dovrebbe', 'farebbe',
            'riuscite', 'riesco', 'riesci', 'posso', 'puoi', 'può', 'possiamo', 'potete',
            'voglio', 'vuoi', 'vuole', 'vogliamo', 'volete', 'vogliono',
            'link', 'meet', 'zoom', 'teams', 'chat', 'gruppo', 'canale', 'messaggio',
            'whatsapp', 'telegram', 'email', 'file', 'pdf', 'video', 'audio',
            
            # NUOVE AGGIUNTE - Verbi comuni
            'fai', 'faccio', 'fa', 'fanno', 'fare', 'fatto', 'fatta', 'fatti', 'fatte',
            'vai', 'vado', 'va', 'vanno', 'andare', 'andato', 'andata', 'andati', 'andate',
            'dai', 'do', 'da', 'danno', 'dare', 'dato', 'data', 'dati', 'date',
            'stai', 'sto', 'sta', 'stanno', 'stare', 'stato', 'stata', 'stati', 'state',
            'sai', 'so', 'sa', 'sanno', 'sapere', 'saputo', 'saputa', 'saputi', 'sapute',
            'entri', 'entro', 'entra', 'entrano', 'entrare', 'entrato', 'entrata', 'entrati', 'entrate',
            'esci', 'esco', 'esce', 'escono', 'uscire', 'uscito', 'uscita', 'usciti', 'uscite',
            'vieni', 'vengo', 'viene', 'vengono', 'venire', 'venuto', 'venuta', 'venuti', 'venute',
            'prendi', 'prendo', 'prende', 'prendono', 'prendere', 'preso', 'presa', 'presi', 'prese',
            'metti', 'metto', 'mette', 'mettono', 'mettere', 'messo', 'messa', 'messi', 'messe',
            'dici', 'dico', 'dice', 'dicono', 'dire', 'detto', 'detta', 'detti', 'dette',
            'vedi', 'vedo', 'vede', 'vedono', 'vedere', 'visto', 'vista', 'visti', 'viste',
            'senti', 'sento', 'sente', 'sentono', 'sentire', 'sentito', 'sentita', 'sentiti', 'sentite',
            'parti', 'parto', 'parte', 'partono', 'partire', 'partito', 'partita', 'partiti', 'partite',
            'torni', 'torno', 'torna', 'tornano', 'tornare', 'tornato', 'tornata', 'tornati', 'tornate',
            'riesci', 'riesco', 'riesce', 'riescono', 'riuscire', 'riuscito', 'riuscita', 'riusciti', 'riuscite',
            'capisci', 'capisco', 'capisce', 'capiscono', 'capire', 'capito', 'capita', 'capiti', 'capite',
            'scrivi', 'scrivo', 'scrive', 'scrivono', 'scrivere', 'scritto', 'scritta', 'scritti', 'scritte',
            'leggi', 'leggo', 'legge', 'leggono', 'leggere', 'letto', 'letta', 'letti', 'lette',
            'lavori', 'lavoro', 'lavora', 'lavorano', 'lavorare', 'lavorato', 'lavorata', 'lavorati', 'lavorate',
            'studi', 'studio', 'studia', 'studiano', 'studiare', 'studiato', 'studiata', 'studiati', 'studiate',
            'giochi', 'gioco', 'gioca', 'giocano', 'giocare', 'giocato', 'giocata', 'giocati', 'giocate',
            'mangi', 'mangio', 'mangia', 'mangiano', 'mangiare', 'mangiato', 'mangiata', 'mangiati', 'mangiate',
            'bevi', 'bevo', 'beve', 'bevono', 'bere', 'bevuto', 'bevuta', 'bevuti', 'bevute',
            'dormi', 'dormo', 'dorme', 'dormono', 'dormire', 'dormito', 'dormita', 'dormiti', 'dormite',
            'svegli', 'sveglio', 'sveglia', 'svegliano', 'svegliare', 'svegliato', 'svegliata', 'svegliati', 'svegliate',
            'chiami', 'chiamo', 'chiama', 'chiamano', 'chiamare', 'chiamato', 'chiamata', 'chiamati', 'chiamate',
            'cerchi', 'cerco', 'cerca', 'cercano', 'cercare', 'cercato', 'cercata', 'cercati', 'cercate',
            'trovi', 'trovo', 'trova', 'trovano', 'trovare', 'trovato', 'trovata', 'trovati', 'trovate',
            'perdi', 'perdo', 'perde', 'perdono', 'perdere', 'perso', 'persa', 'persi', 'perse',
            'vinci', 'vinco', 'vince', 'vincono', 'vincere', 'vinto', 'vinta', 'vinti', 'vinte',
            'compri', 'compro', 'compra', 'comprano', 'comprare', 'comprato', 'comprata', 'comprati', 'comprate',
            'vendi', 'vendo', 'vende', 'vendono', 'vendere', 'venduto', 'venduta', 'venduti', 'vendute',
            'paghi', 'pago', 'paga', 'pagano', 'pagare', 'pagato', 'pagata', 'pagati', 'pagate',
            'spendi', 'spendo', 'spende', 'spendono', 'spendere', 'speso', 'spesa', 'spesi', 'spese',
            'guadagni', 'guadagno', 'guadagna', 'guadagnano', 'guadagnare', 'guadagnato', 'guadagnata', 'guadagnati', 'guadagnate',
            
            # Parole di collegamento e particelle
            'della', 'dello', 'delle', 'degli', 'nel', 'nella', 'nei', 'nelle', 'sul', 'sulla', 'sui', 'sulle',
            'dal', 'dalla', 'dai', 'dalle', 'al', 'alla', 'agli', 'alle', 'col', 'colla', 'colle', 'coi',
            'di', 'da', 'in', 'su', 'tra', 'fra', 'dentro', 'fuori', 'sopra', 'sotto', 'accanto', 'dietro',
            'davanti', 'prima', 'dopo', 'durante', 'mentre', 'intanto', 'poi', 'infine', 'alla', 'fine',
            'ci', 'vi', 'ne', 'lo', 'la', 'li', 'le', 'gli', 'mi', 'ti', 'si', 'se',
            'uno', 'una', 'due', 'tre', 'quattro', 'cinque', 'sei', 'sette', 'otto', 'nove', 'dieci',
            
            # Aggettivi comuni
            'buono', 'buona', 'buoni', 'buone', 'cattivo', 'cattiva', 'cattivi', 'cattive',
            'grande', 'grandi', 'piccolo', 'piccola', 'piccoli', 'piccole',
            'nuovo', 'nuova', 'nuovi', 'nuove', 'vecchio', 'vecchia', 'vecchi', 'vecchie',
            'giovane', 'giovani', 'bello', 'bella', 'belli', 'belle', 'brutto', 'brutta', 'brutti', 'brutte',
            'facile', 'facili', 'difficile', 'difficili', 'semplice', 'semplici', 'complicato', 'complicata', 'complicati', 'complicate',
            'veloce', 'veloci', 'lento', 'lenta', 'lenti', 'lente',
            'alto', 'alta', 'alti', 'alte', 'basso', 'bassa', 'bassi', 'basse',
            'lungo', 'lunga', 'lunghi', 'lunghe', 'corto', 'corta', 'corti', 'corte',
            'largo', 'larga', 'larghi', 'larghe', 'stretto', 'stretta', 'stretti', 'strette',
            'pieno', 'piena', 'pieni', 'piene', 'vuoto', 'vuota', 'vuoti', 'vuote',
            'caldo', 'calda', 'caldi', 'calde', 'freddo', 'fredda', 'freddi', 'fredde',
            'secco', 'secca', 'secchi', 'secche', 'bagnato', 'bagnata', 'bagnati', 'bagnate',
            'pulito', 'pulita', 'puliti', 'pulite', 'sporco', 'sporca', 'sporchi', 'sporche',
            'ricco', 'ricca', 'ricchi', 'ricche', 'povero', 'povera', 'poveri', 'povere',
            'felice', 'felici', 'triste', 'tristi', 'allegro', 'allegra', 'allegri', 'allegre',
            'arrabbiato', 'arrabbiata', 'arrabbiati', 'arrabbiate', 'calmo', 'calma', 'calmi', 'calme',
            'sicuro', 'sicura', 'sicuri', 'sicure', 'incerto', 'incerta', 'incerti', 'incerte',
            'importante', 'importanti', 'inutile', 'inutili', 'utile', 'utili',
            'possibile', 'possibili', 'impossibile', 'impossibili',
            'giusto', 'giusta', 'giusti', 'giuste', 'sbagliato', 'sbagliata', 'sbagliati', 'sbagliate',
            
            # Sostantivi comuni
            'casa', 'case', 'famiglia', 'famiglie', 'persona', 'persone', 'gente',
            'uomo', 'uomini', 'donna', 'donne', 'bambino', 'bambini', 'bambina', 'bambine',
            'ragazzo', 'ragazzi', 'ragazza', 'ragazze', 'amico', 'amici', 'amica', 'amiche',
            'lavoro', 'lavori', 'scuola', 'scuole', 'strada', 'strade', 'città', 'paese', 'paesi',
            'tempo', 'tempi', 'anno', 'anni', 'mese', 'mesi', 'settimana', 'settimane',
            'giorno', 'giorni', 'ora', 'ore', 'minuto', 'minuti', 'secondo', 'secondi',
            'mondo', 'mondi', 'vita', 'vite', 'morte', 'morti', 'salute', 'malattia', 'malattie',
            'soldi', 'denaro', 'euro', 'dollaro', 'dollari', 'prezzo', 'prezzi', 'costo', 'costi',
            'macchina', 'macchine', 'auto', 'treno', 'treni', 'aereo', 'aerei', 'nave', 'navi',
            'telefono', 'telefoni', 'computer', 'internet', 'sito', 'siti', 'pagina', 'pagine',
            'libro', 'libri', 'giornale', 'giornali', 'rivista', 'riviste', 'film', 'cinema',
            'musica', 'canzone', 'canzoni', 'sport', 'calcio', 'tennis', 'palestra', 'palestre',
            'cibo', 'cibi', 'pranzo', 'cena', 'colazione', 'merenda', 'pizza', 'pasta',
            'acqua', 'vino', 'vini', 'birra', 'birre', 'caffè', 'tè', 'latte',
            'camera', 'camere', 'cucina', 'cucine', 'bagno', 'bagni', 'giardino', 'giardini',
            'ufficio', 'uffici', 'negozio', 'negozi', 'supermercato', 'supermercati',
            'ospedale', 'ospedali', 'medico', 'medici', 'dottore', 'dottori', 'dottoressa', 'dottoresse',
            'problema', 'problemi', 'soluzione', 'soluzioni', 'domanda', 'domande', 'risposta', 'risposte',
            'idea', 'idee', 'pensiero', 'pensieri', 'opinione', 'opinioni', 'consiglio', 'consigli',
            'aiuto', 'errore', 'errori', 'sbaglio', 'sbagli', 'successo', 'successi',
            'inizio', 'inizi', 'fine', 'fini', 'centro', 'centri', 'posto', 'posti', 'luogo', 'luoghi',
            'modo', 'modi', 'tipo', 'tipi', 'specie', 'genere', 'generi', 'forma', 'forme',
            
            # Espressioni colloquiali e interiezioni
            'basta', 'dai', 'beh', 'mah', 'boh', 'vabbè', 'vabe', 'ecco', 'eh', 'ah', 'oh',
            'uffa', 'oddio', 'madonna', 'cristo', 'diamine', 'cavolo', 'cazzo', 'merda',
            'figurati', 'immagina', 'pensa', 'senti', 'guarda', 'ascolta',
            'tipo', 'cioè', 'insomma', 'praticamente', 'fondamentalmente', 'sostanzialmente',
            'ovviamente', 'chiaramente', 'evidentemente', 'probabilmente', 'possibilmente',
            'tranquillo', 'tranquilla', 'calmo', 'calma', 'piano', 'attento', 'attenta',
            
            # Risate e espressioni di divertimento
            'ahahah', 'ahaha', 'ahahaha', 'hahaha', 'haha', 'hihi', 'hehe', 'eheh', 'ihih',
            'lol', 'XD', 'xd', 'ahahahah', 'hehehehe', 'eheheh',
            
            # Giorni, mesi, stagioni
            'lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica',
            'gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno',
            'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre',
            'primavera', 'estate', 'autunno', 'inverno',
            'mattina', 'mattino', 'pomeriggio', 'sera', 'notte', 'mezzogiorno', 'mezzanotte',
            
            # Termini universitari specifici
            'università', 'uni', 'ateneo', 'campus', 'facoltà', 'dipartimento', 'cattedra',
            'corso', 'corsi', 'materia', 'materie', 'disciplina', 'discipline',
            'esame', 'esami', 'prova', 'prove', 'test', 'verifica', 'verifiche',
            'voto', 'voti', 'valutazione', 'valutazioni', 'giudizio', 'giudizi',
            'crediti', 'cfu', 'ects', 'ore', 'frequenza', 'obbligo', 'obblighi',
            'laurea', 'lauree', 'triennale', 'magistrale', 'specialistica', 'dottorato',
            'tesi', 'tesina', 'tesine', 'relazione', 'relazioni', 'elaborato', 'elaborati',
            'professore', 'professori', 'prof', 'docente', 'docenti', 'ricercatore', 'ricercatori',
            'assistente', 'assistenti', 'tutor', 'relatore', 'relatori', 'correlatore', 'correlatori',
            'studente', 'studenti', 'studentessa', 'studentesse', 'matricola', 'matricole',
            'iscrizione', 'iscrizioni', 'immatricolazione', 'immatricolazioni',
            'sessione', 'sessioni', 'appello', 'appelli', 'prenotazione', 'prenotazioni',
            'aula', 'aule', 'biblioteca', 'biblioteche', 'laboratorio', 'laboratori',
            'lezione', 'lezioni', 'seminario', 'seminari', 'conferenza', 'conferenze',
            'slide', 'slides', 'dispensa', 'dispense', 'appunti', 'materiale', 'materiali',
            'paniere', 'panieri', 'questionario', 'questionari', 'quiz', 'domanda', 'domande',
            'risposta', 'risposte', 'soluzione', 'soluzioni', 'spiegazione', 'spiegazioni',
            'riassunto', 'riassunti', 'schema', 'schemi', 'mappa', 'mappe',
            'studio', 'studi', 'preparazione', 'preparazioni', 'ripasso', 'ripassi',
            'gruppo', 'gruppi', 'squadra', 'squadre', 'team', 'progetto', 'progetti',
            'ricerca', 'ricerche', 'analisi', 'report', 'presentazione', 'presentazioni',
            'stage', 'tirocinio', 'tirocini', 'pratica', 'pratiche', 'esperienza', 'esperienze',
            'borsa', 'borse', 'erasmus', 'scambio', 'scambi', 'estero',
            'mensa', 'mense', 'bar', 'caffetteria', 'caffetterie', 'segreteria', 'segreterie',
            'ufficio', 'uffici', 'sportello', 'sportelli', 'servizio', 'servizi',
            'certificato', 'certificati', 'diploma', 'diplomi', 'pergamena', 'pergamene',
            'calendario', 'calendari', 'orario', 'orari', 'programma', 'programmi',
            'piano', 'piani', 'curriculum', 'percorso', 'percorsi', 'indirizzo', 'indirizzi',
            
            # Tecnologia e digital
            'computer', 'pc', 'laptop', 'tablet', 'smartphone', 'cellulare', 'telefono',
            'internet', 'rete', 'reti', 'wifi', 'connessione', 'connessioni',
            'sito', 'siti', 'website', 'pagina', 'pagine', 'link', 'collegamenti',
            'email', 'mail', 'messaggio', 'messaggi', 'chat', 'whatsapp', 'telegram',
            'facebook', 'instagram', 'twitter', 'youtube', 'google', 'zoom', 'teams',
            'file', 'files', 'documento', 'documenti', 'pdf', 'word', 'excel', 'powerpoint',
            'foto', 'fotografia', 'fotografie', 'immagine', 'immagini', 'video', 'audio',
            'app', 'applicazione', 'applicazioni', 'software', 'programma', 'programmi',
            'sistema', 'sistemi', 'piattaforma', 'piattaforme', 'servizio', 'servizi',
            'account', 'profilo', 'profili', 'utente', 'utenti', 'password', 'accesso',
            'download', 'upload', 'scarica', 'scaricamento', 'caricamento',
            'online', 'offline', 'digitale', 'digitali', 'virtuale', 'virtuali'
        }
        italian_patterns = [
            r'\w+mente', r'\w+zione', r'\w+zioni', r'\w+aggio',
            r'\w+are', r'\w+ere', r'\w+ire',
            r'\w+amo', r'\w+ete', r'\w+ano',
            r'\w+oso', r'\w+osa', r'\w+osi', r'\w+ose',
            r'[a-z]*uscir\w*', r'[a-z]*vot\w*',
        ]
        words_in_text_lower_no_punct = set(re.sub(r'[^\w\s]', '', word.lower()) for word in clean_text.split())
        words_in_text_lower_no_punct = {word for word in words_in_text_lower_no_punct if word}

        def normalize_repeated_chars(word):
            return re.sub(r'(.)+', r'', word)

        found_strong_italian_indicator = False
        if words_in_text_lower_no_punct.intersection(italian_indicators):
            self.logger.debug(f"✅ Italiano CONFERMATO (indicatore diretto: {words_in_text_lower_no_punct.intersection(italian_indicators)}) per: '{clean_text[:100]}...'")
            found_strong_italian_indicator = True
        
        if not found_strong_italian_indicator:
            normalized_words_for_check = {normalize_repeated_chars(word) for word in words_in_text_lower_no_punct}
            if normalized_words_for_check.intersection(italian_indicators):
                self.logger.debug(f"✅ Italiano CONFERMATO (indicatore post-normalizzazione-ripetuti: {normalized_words_for_check.intersection(italian_indicators)}) per: '{clean_text[:100]}...'")
                found_strong_italian_indicator = True

        if found_strong_italian_indicator:
            return False

        text_for_patterns = unidecode.unidecode(clean_text.lower())
        for pattern in italian_patterns:
            if re.search(pattern, text_for_patterns):
                matches = re.findall(pattern, text_for_patterns)
                self.logger.debug(f"✅ Italiano CONFERMATO (pattern '{pattern}': {matches}) per: '{clean_text[:100]}...'")
                return False

        cyrillic_chars = sum(1 for char in clean_text if 'Ѐ' <= char <= 'ӿ' or 'Ԁ' <= char <= 'ԯ')
        arabic_chars = sum(1 for char in clean_text if '؀' <= char <= 'ۿ')
        chinese_chars = sum(1 for char in clean_text if '一' <= char <= '鿿')
        total_alpha_chars_original = len([c for c in clean_text if c.isalpha()])

        if total_alpha_chars_original > 0:
            non_latin_ratio = (cyrillic_chars + arabic_chars + chinese_chars) / total_alpha_chars_original
            if non_latin_ratio > 0.3:
                self.logger.info(f"❌ Lingua NON CONSENTITA (rapporto non-latino: {non_latin_ratio:.2%}, caratteri: cyr:{cyrillic_chars}, arab:{arabic_chars}, chin:{chinese_chars}) in '{clean_text[:100]}...'")
                return True

        common_english_only = {
            'hello', 'how', 'are', 'you', 'what', 'where', 'when',
            'why', 'who', 'can', 'could', 'would', 'should', 'will', 'shall',
            'good', 'bad', 'nice', 'great', 'thank', 'thanks', 'please', 'sorry',
            'yes', 'okay', 'welcome', 'bye', 'goodbye', 'see', 'get', 'go',
            'this', 'that', 'there', 'here',
            'have', 'has', 'had', 'was', 'were', 'been', 'being', 'make', 'made',
            'take', 'took', 'come', 'came', 'went', 'going', 'do', 'does', 'did',
            'img', 'src', 'href', 'html', 'php', 'javascript', 'python', 'java', 'css'
        }
        if 2 <= len(words_in_text_lower_no_punct) <= 10:
            english_words_found_in_msg = words_in_text_lower_no_punct.intersection(common_english_only)
            if len(english_words_found_in_msg) == len(words_in_text_lower_no_punct):
                self.logger.info(f"❌ Lingua NON CONSENTITA (probabilmente solo Inglese: {list(english_words_found_in_msg)}) in '{clean_text[:100]}...'")
                return True

        if total_alpha_chars_original >= 20:
            detected_lang_code = self.detect_language(clean_text)
            if detected_lang_code:
                lang_mapping = {'italian': 'it', 'it': 'it'}
                allowed_codes = [lang_mapping.get(lang.lower(), lang.lower()) for lang in self.allowed_languages]
                if detected_lang_code not in allowed_codes:
                    italian_words_found_set_for_fallback = words_in_text_lower_no_punct.intersection(italian_indicators)
                    if len(words_in_text_lower_no_punct) > 0:
                        italian_word_ratio_in_fallback = len(italian_words_found_set_for_fallback) / len(words_in_text_lower_no_punct)
                        if italian_word_ratio_in_fallback >= 0.20 or \
                           (len(italian_words_found_set_for_fallback) >= 2 and len(words_in_text_lower_no_punct) < 10):
                            self.logger.info(f"✅ Langdetect ha rilevato '{detected_lang_code}', ma forte presenza italiana ({italian_word_ratio_in_fallback:.2%}, parole: {list(italian_words_found_set_for_fallback)}) sovrascrive. PERMESSO: '{clean_text[:100]}...'")
                            return False
                    self.logger.info(f"❌ Lingua NON CONSENTITA (Langdetect: '{detected_lang_code}', consentite: {allowed_codes}) per '{clean_text[:100]}...'")
                    return True
        else:
            self.logger.debug(f"✅ Testo troppo breve per Langdetect ({total_alpha_chars_original} caratteri alfabetici < 20) o langdetect non disponibile/fallito. Decisione basata su regole precedenti. PERMESSO (default).")
        self.logger.debug(f"✅ Lingua CONSENTITA (default, nessuna regola di blocco attivata) per: '{clean_text[:100]}...'")
        return False

    def update_system_prompt(self, new_prompt: str) -> bool:
        """Aggiorna il system prompt in runtime."""
        try:
            if hasattr(self, 'prompt_manager') and self.prompt_manager:
                return self.prompt_manager.update_prompt(new_prompt)
            return False
        except Exception as e:
            self.logger.error(f"Errore aggiornamento prompt: {e}")
            return False

    def analyze_with_openai(self, message_text: str) -> Tuple[bool, bool, bool]:
        if not self.openai_client:
            self.logger.warning("OpenAI client non disponibile. Analisi AI saltata.")
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            final_is_disallowed_language = self.is_language_disallowed(message_text)
            return is_inappropriate_local, False, final_is_disallowed_language

        final_is_disallowed_language = self.is_language_disallowed(message_text)

        if len(message_text.strip()) <= 10 or re.match(r'^[^\w\s]+$', message_text.strip()):
            self.logger.debug(f"Messaggio breve '{message_text[:20]}' skip analisi AI (contenuto/domanda). Lingua disallow (locale): {final_is_disallowed_language}")
            return False, False, final_is_disallowed_language

        self.stats['total_messages_analyzed_by_openai'] += 1
        
        cached_result_raw = self.analysis_cache.get(message_text)
        if cached_result_raw:
            self.stats['openai_cache_hits'] += 1
            cached_is_inappropriate, cached_is_question, _ = cached_result_raw
            self.logger.debug(f"Risultato analisi (contenuto/domanda) da cache per: '{message_text[:50]}...'. Lingua ricalcolata localmente: {final_is_disallowed_language}")
            actual_tuple_to_return = (cached_is_inappropriate, cached_is_question, final_is_disallowed_language)
            self.analysis_cache.set(message_text, actual_tuple_to_return)
            return actual_tuple_to_return

        self.stats['openai_requests'] += 1
        system_prompt = self.prompt_manager.get_current_prompt()
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message_text}
                ],
                temperature=0.0,
                max_tokens=50,
                timeout=15
            )
            result_text = response.choices[0].message.content.strip() if response.choices[0].message.content else ""
            self.logger.debug(f"Risposta OpenAI: '{result_text}' per messaggio: '{message_text[:50]}...'")
            is_inappropriate_ai = "INAPPROPRIATO: SI" in result_text
            is_question_ai = "DOMANDA: SI" in result_text
            if is_inappropriate_ai or final_is_disallowed_language :
                 self.stats['ai_filter_violations'] +=1
            analysis_tuple = (is_inappropriate_ai, is_question_ai, final_is_disallowed_language)
            self.analysis_cache.set(message_text, analysis_tuple)
            return analysis_tuple
        except OpenAIError as e:
            self.logger.error(f"Errore API OpenAI: {e}", exc_info=True)
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            return is_inappropriate_local, False, final_is_disallowed_language
        except Exception as e:
            self.logger.error(f"Errore imprevisto durante l'analisi OpenAI: {e}", exc_info=True)
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            return is_inappropriate_local, False, final_is_disallowed_language
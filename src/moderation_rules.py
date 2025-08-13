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
        """
        Determina se un testo è in una lingua non consentita.
        VERSIONE CORRETTA - Risolve i falsi positivi per frasi italiane comuni.
        """
        if not self.allowed_languages or "any" in self.allowed_languages:
            return False
        
        clean_text = text.strip()
        if not clean_text:
            return False
        
        self.logger.debug(f"ℹ️ ANALISI LINGUA per: '{clean_text[:100]}...'")
        
        # AMPLIAMENTO SIGNIFICATIVO degli indicatori italiani
        italian_indicators = {
            # Articoli, preposizioni, congiunzioni (molto comuni)
            'il', 'la', 'lo', 'le', 'gli', 'i', 'un', 'una', 'uno',
            'del', 'della', 'dello', 'delle', 'degli', 'dei',
            'nel', 'nella', 'nello', 'nelle', 'negli', 'nei',
            'dal', 'dalla', 'dallo', 'dalle', 'dagli', 'dai',
            'al', 'alla', 'allo', 'alle', 'agli', 'ai',
            'sul', 'sulla', 'sullo', 'sulle', 'sugli', 'sui',
            'con', 'per', 'tra', 'fra', 'di', 'da', 'in', 'a', 'su',
            'che', 'chi', 'cui', 'quale', 'quanto', 'dove', 'quando', 'come', 'perché',
            'e', 'ed', 'o', 'od', 'ma', 'però', 'tuttavia', 'quindi', 'allora', 'infatti',
            
            # Pronomi comuni
            'io', 'tu', 'lui', 'lei', 'noi', 'voi', 'loro',
            'mi', 'ti', 'ci', 'vi', 'si', 'lo', 'la', 'li', 'le', 'ne',
            'me', 'te', 'se', 'lui', 'lei', 'noi', 'voi', 'loro',
            'questo', 'questa', 'questi', 'queste', 'quello', 'quella', 'quelli', 'quelle',
            
            # Verbi essere/avere (essenziali)
            'è', 'sono', 'sei', 'siamo', 'siete', 'era', 'ero', 'eri', 'erano', 'eravamo', 'eravate',
            'sarà', 'sarò', 'sarai', 'saremo', 'sarete', 'saranno', 'sia', 'siano', 'fosse', 'fossero',
            'ho', 'hai', 'ha', 'abbiamo', 'avete', 'hanno', 'aveva', 'avevo', 'avevi', 'avevamo', 'avevate', 'avevano',
            
            # Verbi comuni
            'fai', 'faccio', 'fa', 'fanno', 'fare', 'fatto', 'fatta', 'fatti', 'fatte',
            'vai', 'vado', 'va', 'vanno', 'andare', 'andato', 'andata', 'andati', 'andate',
            'dai', 'do', 'da', 'danno', 'dare', 'dato', 'data', 'dati', 'date',
            'dici', 'dico', 'dice', 'dicono', 'dire', 'detto', 'detta', 'detti', 'dette',
            'vedi', 'vedo', 'vede', 'vedono', 'vedere', 'visto', 'vista', 'visti', 'viste',
            'senti', 'sento', 'sente', 'sentono', 'sentire', 'sentito', 'sentita', 'sentiti', 'sentite',
            'vengo', 'vieni', 'viene', 'veniamo', 'venite', 'vengono', 'venire', 'venuto', 'venuta',
            'posso', 'puoi', 'può', 'possiamo', 'potete', 'possono', 'potere', 'potuto', 'potuta',
            'voglio', 'vuoi', 'vuole', 'vogliamo', 'volete', 'vogliono', 'volere', 'voluto', 'voluta',
            'devo', 'devi', 'deve', 'dobbiamo', 'dovete', 'devono', 'dovere', 'dovuto', 'dovuta',
            
            # Parole di uso quotidiano
            'ciao', 'buongiorno', 'buonasera', 'buonanotte', 'salve', 'arrivederci',
            'grazie', 'prego', 'scusa', 'scusate', 'perfetto', 'bene', 'male', 'così',
            'si', 'sì', 'no', 'ok', 'okay', 'boh', 'mah', 'beh', 'allora',
            'oggi', 'ieri', 'domani', 'ora', 'adesso', 'sempre', 'mai', 'già', 'ancora',
            'molto', 'poco', 'tanto', 'troppo', 'più', 'meno', 'tutto', 'niente', 'nulla',
            'anche', 'solo', 'proprio', 'davvero', 'veramente', 'sicuramente', 'forse', 'magari',
            
            # AGGIUNTE SPECIFICHE per i casi problematici
            'gestione', 'periodo', 'estenderanno', 'oscena', 'questa', 'quella',
            'momento', 'situazione', 'problema', 'soluzione', 'informazione', 'comunicazione',
            'decisione', 'discussione', 'questione', 'posizione', 'condizione', 'attenzione',
            'direzione', 'protezione', 'produzione', 'costruzione', 'istruzione', 'educazione',
            
            # Ambito universitario
            'università', 'professore', 'prof', 'crediti', 'corso', 'corsi', 'esame', 'esami',
            'laurea', 'triennale', 'magistrale', 'dottorato', 'facoltà', 'appunti', 'paniere', 'panieri',
            'lezioni', 'tesi', 'sessione', 'matricola', 'ateneo', 'dipartimento', 'cattedra',
            'semestre', 'frequenza', 'iscrizione', 'slides', 'slide', 'docente', 'studente',
            'voti', 'voto', 'votazione', 'valutazione', 'risultato', 'risultati',
            
            # Forme verbali comuni che potrebbero essere confuse
            'resta', 'restano', 'restare', 'rimanere', 'rimane', 'rimangono',
            'passa', 'passano', 'passare', 'passato', 'passata', 'passati', 'passate',
            'prende', 'prendono', 'prendere', 'preso', 'presa', 'presi', 'prese',
            'mette', 'mettono', 'mettere', 'messo', 'messa', 'messi', 'messe',
            'porta', 'portano', 'portare', 'portato', 'portata', 'portati', 'portate',
            'trova', 'trovano', 'trovare', 'trovato', 'trovata', 'trovati', 'trovate',
            'cerca', 'cercano', 'cercare', 'cercato', 'cercata', 'cercati', 'cercate',
            'chiede', 'chiedono', 'chiedere', 'chiesto', 'chiesta', 'chiesti', 'chieste',
            'risponde', 'rispondono', 'rispondere', 'risposto', 'risposta', 'risposti', 'risposte',
            'serve', 'servono', 'servire', 'servito', 'servita', 'serviti', 'servite',
            'aiuta', 'aiutano', 'aiutare', 'aiutato', 'aiutata', 'aiutati', 'aiutate',
            'funziona', 'funzionano', 'funzionare', 'funzionato', 'funzionata',
            'cambia', 'cambiano', 'cambiare', 'cambiato', 'cambiata', 'cambiati', 'cambiate',
            'aspetta', 'aspettano', 'aspettare', 'aspettato', 'aspettata', 'aspettati', 'aspettate',
            'finisce', 'finiscono', 'finire', 'finito', 'finita', 'finiti', 'finite',
            'inizia', 'iniziano', 'iniziare', 'iniziato', 'iniziata', 'iniziati', 'iniziate',
            'continua', 'continuano', 'continuare', 'continuato', 'continuata',
            'smette', 'smettono', 'smettere', 'smesso', 'smessa', 'smessi', 'smesse',
            'cresce', 'crescono', 'crescere', 'cresciuto', 'cresciuta', 'cresciuti', 'cresciute',
            'scende', 'scendono', 'scendere', 'sceso', 'scesa', 'scesi', 'scese',
            'sale', 'salgono', 'salire', 'salito', 'salita', 'saliti', 'salite',
            'esce', 'escono', 'uscire', 'uscito', 'uscita', 'usciti', 'uscite',
            'entra', 'entrano', 'entrare', 'entrato', 'entrata', 'entrati', 'entrate',
            'parte', 'partono', 'partire', 'partito', 'partita', 'partiti', 'partite',
            'torna', 'tornano', 'tornare', 'tornato', 'tornata', 'tornati', 'tornate',
            'arriva', 'arrivano', 'arrivare', 'arrivato', 'arrivata', 'arrivati', 'arrivate',
            
            # Avverbi e aggettivi comuni
            'nuovo', 'nuova', 'nuovi', 'nuove', 'vecchio', 'vecchia', 'vecchi', 'vecchie',
            'grande', 'grandi', 'piccolo', 'piccola', 'piccoli', 'piccole',
            'bello', 'bella', 'belli', 'belle', 'brutto', 'brutta', 'brutti', 'brutte',
            'buono', 'buona', 'buoni', 'buone', 'cattivo', 'cattiva', 'cattivi', 'cattive',
            'primo', 'prima', 'primi', 'prime', 'ultimo', 'ultima', 'ultimi', 'ultime',
            'stesso', 'stessa', 'stessi', 'stesse', 'altro', 'altra', 'altri', 'altre',
            'importante', 'importanti', 'interessante', 'interessanti', 'difficile', 'difficili',
            'facile', 'facili', 'possibile', 'possibili', 'impossibile', 'impossibili',
            'necessario', 'necessaria', 'necessari', 'necessarie', 'libero', 'libera', 'liberi', 'libere',
            'aperto', 'aperta', 'aperti', 'aperte', 'chiuso', 'chiusa', 'chiusi', 'chiuse',
            'pieno', 'piena', 'pieni', 'piene', 'vuoto', 'vuota', 'vuoti', 'vuote',
            'alto', 'alta', 'alti', 'alte', 'basso', 'bassa', 'bassi', 'basse',
            'lungo', 'lunga', 'lunghi', 'lunghe', 'corto', 'corta', 'corti', 'corte',
            'largo', 'larga', 'larghi', 'larghe', 'stretto', 'stretta', 'stretti', 'strette',
            'giovane', 'giovani', 'vecchio', 'vecchia', 'vecchi', 'vecchie',
            'ricco', 'ricca', 'ricchi', 'ricche', 'povero', 'povera', 'poveri', 'povere',
            'felice', 'felici', 'triste', 'tristi', 'contento', 'contenta', 'contenti', 'contente',
            'sicuro', 'sicura', 'sicuri', 'sicure', 'incerto', 'incerta', 'incerti', 'incerte',
            'pronto', 'pronta', 'pronti', 'pronte', 'lento', 'lenta', 'lenti', 'lente',
            'veloce', 'veloci', 'rapido', 'rapida', 'rapidi', 'rapide',
            'calmo', 'calma', 'calmi', 'calme', 'nervoso', 'nervosa', 'nervosi', 'nervose',
            'tranquillo', 'tranquilla', 'tranquilli', 'tranquille', 'agitato', 'agitata', 'agitati', 'agitate',
            'normale', 'normali', 'strano', 'strana', 'strani', 'strane',
            'giusto', 'giusta', 'giusti', 'giuste', 'sbagliato', 'sbagliata', 'sbagliati', 'sbagliate',
            'vero', 'vera', 'veri', 'vere', 'falso', 'falsa', 'falsi', 'false',
            'serio', 'seria', 'seri', 'serie', 'scherzoso', 'scherzosa', 'scherzosi', 'scherzose',
            'pubblico', 'pubblica', 'pubblici', 'pubbliche', 'privato', 'privata', 'privati', 'private',
            'sociale', 'sociali', 'personale', 'personali', 'generale', 'generali', 'particolare', 'particolari',
            'nazionale', 'nazionali', 'internazionale', 'internazionali', 'locale', 'locali', 'regionale', 'regionali',
            
            # Termini tecnici e digitali comuni
            'computer', 'internet', 'sito', 'email', 'telefono', 'cellulare', 'numero', 'link', 'collegamento',
            'file', 'documento', 'foto', 'immagine', 'video', 'audio', 'messaggio', 'testo', 'pagina',
            'gruppo', 'canale', 'chat', 'whatsapp', 'telegram', 'facebook', 'instagram', 'youtube',
            'google', 'gmail', 'microsoft', 'windows', 'android', 'iphone', 'apple', 'samsung',
            'password', 'utente', 'account', 'profilo', 'accesso', 'registrazione', 'login',
            'download', 'upload', 'installare', 'installato', 'installata', 'aggiornare', 'aggiornato', 'aggiornata',
            'configurare', 'configurato', 'configurata', 'impostare', 'impostato', 'impostata',
            'connessione', 'connesso', 'connessa', 'disconnesso', 'disconnessa', 'collegato', 'collegata',
            'online', 'offline', 'digitale', 'virtuale', 'elettronico', 'elettronica',
            'automatico', 'automatica', 'manuale', 'manuali', 'sistema', 'sistemi', 'programma', 'programmi',
            'applicazione', 'applicazioni', 'app', 'software', 'hardware', 'device', 'dispositivo', 'dispositivi',
            'schermo', 'monitor', 'tastiera', 'mouse', 'stampante', 'scanner', 'cavo', 'cavi',
            'batteria', 'caricatore', 'memoria', 'disco', 'usb', 'wifi', 'bluetooth', 'gps',
            
            # Mesi e giorni
            'gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno',
            'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre',
            'lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica',
            'mattina', 'pomeriggio', 'sera', 'notte', 'giorno', 'giorni', 'settimana', 'settimane',
            'mese', 'mesi', 'anno', 'anni', 'tempo', 'volta', 'volte', 'ore', 'minuti', 'secondi',
            
            # Numeri scritti in lettere
            'uno', 'due', 'tre', 'quattro', 'cinque', 'sei', 'sette', 'otto', 'nove', 'dieci',
            'undici', 'dodici', 'tredici', 'quattordici', 'quindici', 'sedici', 'diciassette', 'diciotto', 'diciannove', 'venti',
            'trenta', 'quaranta', 'cinquanta', 'sessanta', 'settanta', 'ottanta', 'novanta', 'cento', 'mille',
            'primo', 'secondo', 'terzo', 'quarto', 'quinto', 'sesto', 'settimo', 'ottavo', 'nono', 'decimo',
            
            # Espressioni e interiezioni
            'ecco', 'allora', 'infatti', 'comunque', 'tuttavia', 'inoltre', 'invece', 'piuttosto',
            'soprattutto', 'specialmente', 'particolarmente', 'principalmente', 'generalmente', 'solitamente',
            'probabilmente', 'possibilmente', 'certamente', 'sicuramente', 'ovviamente', 'naturalmente',
            'sfortunatamente', 'fortunatamente', 'improvvisamente', 'immediatamente', 'velocemente', 'lentamente',
            'attentamente', 'facilmente', 'difficilmente', 'chiaramente', 'completamente', 'perfettamente',
            'abbastanza', 'parecchio', 'piuttosto', 'alquanto', 'estremamente', 'incredibilmente',
            'assolutamente', 'relativamente', 'normalmente', 'regolarmente', 'raramente', 'spesso',
            'talvolta', 'qualche', 'volta', 'alcuni', 'alcune', 'parecchi', 'parecchie', 'molti', 'molte',
            'diversi', 'diverse', 'vari', 'varie', 'certi', 'certe', 'tutti', 'tutte', 'nessuno', 'nessuna',
            'qualcuno', 'qualcuna', 'qualcosa', 'niente', 'nulla', 'ovunque', 'dovunque', 'dappertutto',
            'altrove', 'laggiù', 'lassù', 'quaggiù', 'quassù', 'sopra', 'sotto', 'dentro', 'fuori',
            'davanti', 'dietro', 'accanto', 'vicino', 'lontano', 'intorno', 'attraverso', 'lungo', 'contro',
            'verso', 'fino', 'durante', 'prima', 'dopo', 'mentre', 'quando', 'appena', 'finché',
            'sebbene', 'benché', 'nonostante', 'purché', 'affinché', 'perché', 'poiché', 'siccome',
            'dato', 'visto', 'considerato', 'tranne', 'eccetto', 'salvo', 'oltre', 'incluso', 'compreso'
        }
        
        # Pattern tipicamente italiani (mantenuti dal codice originale)
        italian_patterns = [
            r'\b\w+zione\b',  # -zione (situazione, informazione, etc.)
            r'\b\w+mente\b',  # -mente (ovviamente, chiaramente, etc.)
            r'\b\w+aggio\b',  # -aggio (messaggio, viaggio, etc.)
            r'\b\w+ezza\b',   # -ezza (bellezza, tristezza, etc.)
            r'\b\w+ità\b',    # -ità (città, università, etc.)
            r'\bgli\s+\w+\b', # articolo "gli"
            r'\bdegli\s+\w+\b', # "degli"
            r'\bdella\s+\w+\b', # "della"
            r'\bnella\s+\w+\b', # "nella"
            r'\bche\s+\w+\b',   # congiunzione "che"
            r'\bcon\s+\w+\b',   # preposizione "con"
            r'\bper\s+\w+\b',   # preposizione "per"
            r'\bdi\s+\w+\b',    # preposizione "di"
            r'\bda\s+\w+\b',    # preposizione "da"
            r'\ba\s+\w+\b',     # preposizione "a"
            r'\bin\s+\w+\b',    # preposizione "in"
            r'\bsu\s+\w+\b',    # preposizione "su"
            r'\btra\s+\w+\b',   # preposizione "tra"
            r'\bfra\s+\w+\b',   # preposizione "fra"
            r'\bsono\s+\w+\b',  # verbo "sono"
            r'\bsiamo\s+\w+\b', # verbo "siamo"
            r'\bho\s+\w+\b',    # verbo "ho"
            r'\bhai\s+\w+\b',   # verbo "hai"
            r'\bha\s+\w+\b',    # verbo "ha"
            r'\babbiamo\s+\w+\b', # verbo "abbiamo"
            r'\bavete\s+\w+\b', # verbo "avete"
            r'\bhanno\s+\w+\b', # verbo "hanno"
            r'\bè\s+\w+\b',     # verbo "è"
            r'\bsarà\s+\w+\b',  # verbo "sarà"
            r'\bsono\s+\w+\b',  # verbo "sono"
            r'\bsiete\s+\w+\b', # verbo "siete"
            r'\bquello\s+\w+\b', # pronome "quello"
            r'\bquella\s+\w+\b', # pronome "quella"
            r'\bquesti\s+\w+\b', # pronome "questi"
            r'\bqueste\s+\w+\b', # pronome "queste"
            r'\bquelli\s+\w+\b', # pronome "quelli"
            r'\bquelle\s+\w+\b', # pronome "quelle"
            r'\bquesto\s+\w+\b', # pronome "questo"
            r'\bquesta\s+\w+\b', # pronome "questa"
        ]
        
        # Normalizzazione del testo
        def normalize_repeated_chars(word):
            return re.sub(r'(.)\1+', r'\1', word)
        
        # Estrazione parole (escludendo punteggiatura)
        words_in_text_lower_no_punct = set(re.sub(r'[^\w\s]', '', clean_text.lower()).split())
        words_in_text_lower_no_punct = {word for word in words_in_text_lower_no_punct if word and len(word) > 1}
        
        # CONTROLLO 1: Indicatori italiani diretti
        found_strong_italian_indicator = False
        italian_words_found = words_in_text_lower_no_punct.intersection(italian_indicators)
        if italian_words_found:
            self.logger.debug(f"✅ Italiano CONFERMATO (indicatori diretti: {list(italian_words_found)}) per: '{clean_text[:100]}...'")
            found_strong_italian_indicator = True
        
        # CONTROLLO 2: Indicatori italiani dopo normalizzazione caratteri ripetuti
        if not found_strong_italian_indicator:
            normalized_words_for_check = {normalize_repeated_chars(word) for word in words_in_text_lower_no_punct}
            normalized_italian_found = normalized_words_for_check.intersection(italian_indicators)
            if normalized_italian_found:
                self.logger.debug(f"✅ Italiano CONFERMATO (indicatori post-normalizzazione: {list(normalized_italian_found)}) per: '{clean_text[:100]}...'")
                found_strong_italian_indicator = True
        
        # CONTROLLO 3: Pattern italiani
        if not found_strong_italian_indicator:
            text_for_patterns = unidecode.unidecode(clean_text.lower())
            for pattern in italian_patterns:
                if re.search(pattern, text_for_patterns):
                    matches = re.findall(pattern, text_for_patterns)
                    self.logger.debug(f"✅ Italiano CONFERMATO (pattern '{pattern}': {matches}) per: '{clean_text[:100]}...'")
                    found_strong_italian_indicator = True
                    break
        
        # Se abbiamo trovato indicatori italiani forti, il messaggio è consentito
        if found_strong_italian_indicator:
            return False
        
        # CONTROLLO 4: Caratteri non-latini (cirillico, arabo, cinese)
        cyrillic_chars = sum(1 for char in clean_text if 'Ѐ' <= char <= 'ӿ' or 'Ԁ' <= char <= 'ԯ')
        arabic_chars = sum(1 for char in clean_text if '؀' <= char <= 'ۿ')
        chinese_chars = sum(1 for char in clean_text if '一' <= char <= '鿿')
        total_alpha_chars_original = len([c for c in clean_text if c.isalpha()])
        
        if total_alpha_chars_original > 0:
            non_latin_ratio = (cyrillic_chars + arabic_chars + chinese_chars) / total_alpha_chars_original
            if non_latin_ratio > 0.3:
                self.logger.info(f"❌ Lingua NON CONSENTITA (rapporto non-latino: {non_latin_ratio:.2%}) in '{clean_text[:100]}...'")
                return True
        
        # CONTROLLO 5: Controllo inglese MIGLIORATO (meno aggressivo)
        # Lista di parole inglesi che NON sono ambigue con l'italiano
        strict_english_only = {
            'hello', 'goodbye', 'please', 'thank', 'thanks', 'welcome', 'sorry',
            'how', 'what', 'where', 'when', 'why', 'who', 'which',
            'can', 'could', 'would', 'should', 'will', 'shall', 'might', 'must',
            'have', 'has', 'had', 'was', 'were', 'been', 'being',
            'make', 'made', 'take', 'took', 'come', 'came', 'went', 'going',
            'get', 'got', 'give', 'gave', 'put', 'let', 'set',
            'think', 'thought', 'know', 'knew', 'see', 'saw', 'look', 'tell', 'said',
            'want', 'need', 'like', 'love', 'hate', 'feel', 'seem', 'become',
            'find', 'found', 'keep', 'kept', 'leave', 'left', 'bring', 'brought',
            'turn', 'turned', 'show', 'showed', 'ask', 'asked', 'try', 'tried',
            'use', 'used', 'work', 'worked', 'call', 'called', 'move', 'moved',
            'play', 'played', 'run', 'ran', 'walk', 'walked', 'sit', 'sat',
            'stand', 'stood', 'read', 'write', 'wrote', 'listen', 'heard',
            'speak', 'spoke', 'talk', 'talked', 'eat', 'ate', 'drink', 'drank',
            'sleep', 'slept', 'wake', 'woke', 'open', 'opened', 'close', 'closed',
            'start', 'started', 'stop', 'stopped', 'finish', 'finished', 'end', 'ended',
            'begin', 'began', 'continue', 'continued', 'change', 'changed', 'help', 'helped',
            'learn', 'learned', 'teach', 'taught', 'study', 'studied', 'remember', 'forgot',
            'understand', 'understood', 'believe', 'believed', 'hope', 'hoped', 'wish', 'wished',
            'buy', 'bought', 'sell', 'sold', 'pay', 'paid', 'cost', 'spend', 'spent',
            'live', 'lived', 'stay', 'stayed', 'visit', 'visited', 'travel', 'traveled',
            'meet', 'met', 'join', 'joined', 'follow', 'followed', 'lead', 'led',
            'win', 'won', 'lose', 'lost', 'fight', 'fought', 'kill', 'killed',
            'die', 'died', 'born', 'grow', 'grew', 'build', 'built', 'create', 'created',
            'destroy', 'destroyed', 'break', 'broke', 'fix', 'fixed', 'repair', 'repaired',
            'clean', 'cleaned', 'wash', 'washed', 'cook', 'cooked', 'cut', 'cut',
            'pull', 'pulled', 'push', 'pushed', 'throw', 'threw', 'catch', 'caught',
            'hold', 'held', 'carry', 'carried', 'pick', 'picked', 'drop', 'dropped',
            'send', 'sent', 'receive', 'received', 'give', 'gave', 'take', 'took',
            'choose', 'chose', 'decide', 'decided', 'agree', 'agreed', 'disagree', 'disagreed',
            'accept', 'accepted', 'refuse', 'refused', 'allow', 'allowed', 'permit', 'permitted',
            'forbid', 'forbidden', 'force', 'forced', 'let', 'protect', 'protected',
            'save', 'saved', 'rescue', 'rescued', 'escape', 'escaped', 'avoid', 'avoided',
            'prevent', 'prevented', 'cause', 'caused', 'happen', 'happened', 'occur', 'occurred',
            'exist', 'existed', 'appear', 'appeared', 'disappear', 'disappeared', 'remain', 'remained',
            'contain', 'contained', 'include', 'included', 'involve', 'involved', 'depend', 'depended',
            'belong', 'belonged', 'own', 'owned', 'share', 'shared', 'offer', 'offered',
            'provide', 'provided', 'supply', 'supplied', 'deliver', 'delivered', 'serve', 'served',
            'support', 'supported', 'manage', 'managed', 'control', 'controlled', 'handle', 'handled',
            'deal', 'dealt', 'treat', 'treated', 'care', 'cared', 'worry', 'worried',
            'fear', 'feared', 'surprise', 'surprised', 'shock', 'shocked', 'amaze', 'amazed',
            'confuse', 'confused', 'explain', 'explained', 'describe', 'described', 'discuss', 'discussed',
            'argue', 'argued', 'complain', 'complained', 'suggest', 'suggested', 'recommend', 'recommended',
            'advise', 'advised', 'warn', 'warned', 'remind', 'reminded', 'inform', 'informed',
            'announce', 'announced', 'declare', 'declared', 'claim', 'claimed', 'state', 'stated',
            'mention', 'mentioned', 'report', 'reported', 'confirm', 'confirmed', 'deny', 'denied',
            'admit', 'admitted', 'confess', 'confessed', 'lie', 'lied', 'steal', 'stole',
            'cheat', 'cheated', 'trick', 'tricked', 'fool', 'fooled', 'joke', 'joked',
            'laugh', 'laughed', 'smile', 'smiled', 'cry', 'cried', 'shout', 'shouted',
            'whisper', 'whispered', 'sing', 'sang', 'dance', 'danced', 'jump', 'jumped',
            'climb', 'climbed', 'fall', 'fell', 'fly', 'flew', 'swim', 'swam',
            'drive', 'drove', 'ride', 'rode', 'arrive', 'arrived', 'reach', 'reached',
            'return', 'returned', 'enter', 'entered', 'exit', 'exited', 'approach', 'approached',
            'pass', 'passed', 'cross', 'crossed', 'follow', 'followed', 'chase', 'chased',
            'search', 'searched', 'explore', 'explored', 'discover', 'discovered', 'notice', 'noticed',
            'observe', 'observed', 'watch', 'watched', 'examine', 'examined', 'check', 'checked',
            'test', 'tested', 'try', 'tried', 'attempt', 'attempted', 'practice', 'practiced',
            'train', 'trained', 'exercise', 'exercised', 'compete', 'competed', 'race', 'raced',
            'play', 'played', 'game', 'games', 'sport', 'sports', 'team', 'teams',
            'player', 'players', 'coach', 'coaches', 'fan', 'fans', 'audience', 'audiences',
            'show', 'shows', 'movie', 'movies', 'film', 'films', 'book', 'books',
            'story', 'stories', 'news', 'information', 'data', 'fact', 'facts',
            'idea', 'ideas', 'thought', 'thoughts', 'opinion', 'opinions', 'view', 'views',
            'point', 'points', 'reason', 'reasons', 'cause', 'causes', 'effect', 'effects',
            'result', 'results', 'answer', 'answers', 'question', 'questions', 'problem', 'problems',
            'solution', 'solutions', 'method', 'methods', 'way', 'ways', 'manner', 'manners',
            'style', 'styles', 'type', 'types', 'kind', 'kinds', 'sort', 'sorts',
            'class', 'classes', 'group', 'groups', 'team', 'teams', 'member', 'members',
            'person', 'people', 'human', 'humans', 'man', 'men', 'woman', 'women',
            'child', 'children', 'baby', 'babies', 'boy', 'boys', 'girl', 'girls',
            'family', 'families', 'parent', 'parents', 'mother', 'mothers', 'father', 'fathers',
            'brother', 'brothers', 'sister', 'sisters', 'friend', 'friends', 'enemy', 'enemies',
            'neighbor', 'neighbors', 'stranger', 'strangers', 'guest', 'guests', 'host', 'hosts',
            'customer', 'customers', 'client', 'clients', 'boss', 'bosses', 'worker', 'workers',
            'employee', 'employees', 'employer', 'employers', 'job', 'jobs', 'work', 'works',
            'business', 'businesses', 'company', 'companies', 'office', 'offices', 'store', 'stores',
            'shop', 'shops', 'market', 'markets', 'bank', 'banks', 'school', 'schools',
            'university', 'universities', 'college', 'colleges', 'class', 'classes', 'student', 'students',
            'teacher', 'teachers', 'professor', 'professors', 'doctor', 'doctors', 'nurse', 'nurses',
            'hospital', 'hospitals', 'medicine', 'medicines', 'health', 'healthy', 'sick', 'disease',
            'pain', 'hurt', 'injury', 'accident', 'emergency', 'danger', 'safe', 'safety',
            'police', 'crime', 'law', 'legal', 'court', 'judge', 'jury', 'lawyer',
            'government', 'politics', 'president', 'minister', 'election', 'vote', 'citizen', 'country',
            'nation', 'state', 'city', 'town', 'village', 'place', 'location', 'address',
            'street', 'road', 'avenue', 'building', 'house', 'home', 'apartment', 'room',
            'kitchen', 'bathroom', 'bedroom', 'living', 'garden', 'yard', 'garage', 'basement',
            'floor', 'ceiling', 'wall', 'door', 'window', 'roof', 'stairs', 'elevator',
            'furniture', 'table', 'chair', 'bed', 'sofa', 'desk', 'shelf', 'mirror',
            'picture', 'painting', 'photo', 'image', 'color', 'colors', 'red', 'blue',
            'green', 'yellow', 'orange', 'purple', 'pink', 'brown', 'black', 'white',
            'grey', 'gray', 'light', 'dark', 'bright', 'clear', 'transparent', 'thick',
            'thin', 'wide', 'narrow', 'long', 'short', 'tall', 'high', 'low',
            'big', 'large', 'huge', 'giant', 'small', 'tiny', 'little', 'medium',
            'heavy', 'light', 'strong', 'weak', 'hard', 'soft', 'smooth', 'rough',
            'hot', 'warm', 'cool', 'cold', 'freezing', 'wet', 'dry', 'clean', 'dirty',
            'new', 'old', 'young', 'fresh', 'stale', 'good', 'bad', 'great', 'terrible',
            'wonderful', 'amazing', 'beautiful', 'ugly', 'nice', 'pleasant', 'horrible', 'awful',
            'perfect', 'excellent', 'outstanding', 'poor', 'rich', 'expensive', 'cheap', 'free',
            'easy', 'difficult', 'hard', 'simple', 'complex', 'complicated', 'clear', 'obvious',
            'strange', 'weird', 'normal', 'usual', 'common', 'rare', 'special', 'ordinary',
            'important', 'serious', 'funny', 'interesting', 'boring', 'exciting', 'surprising', 'shocking',
            'happy', 'sad', 'angry', 'mad', 'calm', 'peaceful', 'nervous', 'worried',
            'afraid', 'scared', 'brave', 'proud', 'ashamed', 'embarrassed', 'confident', 'shy',
            'lonely', 'popular', 'famous', 'unknown', 'public', 'private', 'secret', 'open',
            'closed', 'full', 'empty', 'complete', 'incomplete', 'finished', 'ready', 'busy',
            'free', 'available', 'possible', 'impossible', 'necessary', 'optional', 'required', 'forbidden',
            'allowed', 'permitted', 'legal', 'illegal', 'right', 'wrong', 'correct', 'incorrect',
            'true', 'false', 'real', 'fake', 'actual', 'virtual', 'original', 'copy',
            'first', 'second', 'third', 'last', 'final', 'next', 'previous', 'following',
            'single', 'double', 'triple', 'multiple', 'few', 'several', 'many', 'much',
            'more', 'most', 'less', 'least', 'enough', 'too', 'very', 'quite',
            'rather', 'pretty', 'fairly', 'extremely', 'incredibly', 'absolutely', 'completely', 'totally',
            'almost', 'nearly', 'about', 'approximately', 'exactly', 'precisely', 'roughly', 'generally',
            'usually', 'normally', 'typically', 'often', 'sometimes', 'rarely', 'never', 'always',
            'forever', 'temporary', 'permanent', 'constant', 'stable', 'changing', 'moving', 'still',
            'active', 'passive', 'alive', 'dead', 'living', 'dying', 'growing', 'shrinking',
            'increasing', 'decreasing', 'rising', 'falling', 'improving', 'worsening', 'developing', 'declining',
            'successful', 'unsuccessful', 'winning', 'losing', 'leading', 'following', 'ahead', 'behind',
            'early', 'late', 'slow', 'fast', 'quick', 'rapid', 'sudden', 'gradual',
            'immediate', 'instant', 'delayed', 'urgent', 'emergency', 'priority', 'important', 'minor',
            'major', 'main', 'primary', 'secondary', 'basic', 'advanced', 'elementary', 'fundamental',
            'essential', 'necessary', 'optional', 'extra', 'additional', 'spare', 'reserve', 'backup',
            'original', 'duplicate', 'copy', 'version', 'edition', 'model', 'brand', 'type',
            'category', 'section', 'part', 'piece', 'item', 'object', 'thing', 'stuff',
            'material', 'substance', 'element', 'component', 'ingredient', 'content', 'subject', 'topic',
            'theme', 'issue', 'matter', 'affair', 'business', 'concern', 'interest', 'hobby',
            'activity', 'action', 'movement', 'motion', 'behavior', 'conduct', 'attitude', 'approach',
            'method', 'technique', 'skill', 'ability', 'talent', 'gift', 'power', 'strength',
            'weakness', 'advantage', 'disadvantage', 'benefit', 'profit', 'loss', 'gain', 'cost',
            'price', 'value', 'worth', 'quality', 'quantity', 'amount', 'number', 'figure',
            'total', 'sum', 'average', 'minimum', 'maximum', 'limit', 'range', 'scale',
            'level', 'degree', 'grade', 'rank', 'position', 'status', 'condition', 'situation',
            'state', 'circumstance', 'case', 'example', 'instance', 'occasion', 'opportunity', 'chance',
            'possibility', 'probability', 'risk', 'danger', 'threat', 'warning', 'alarm', 'signal',
            'sign', 'symbol', 'mark', 'label', 'tag', 'name', 'title', 'heading',
            'caption', 'description', 'explanation', 'definition', 'meaning', 'sense', 'purpose', 'goal',
            'aim', 'target', 'objective', 'plan', 'strategy', 'tactic', 'approach', 'method',
            'system', 'process', 'procedure', 'operation', 'function', 'role', 'job', 'task',
            'duty', 'responsibility', 'obligation', 'requirement', 'rule', 'regulation', 'policy', 'principle',
            'law', 'standard', 'norm', 'custom', 'tradition', 'culture', 'society', 'community',
            'public', 'audience', 'crowd', 'mass', 'population', 'generation', 'age', 'era',
            'period', 'time', 'moment', 'instant', 'second', 'minute', 'hour', 'day',
            'week', 'month', 'year', 'decade', 'century', 'millennium', 'past', 'present',
            'future', 'history', 'story', 'tale', 'account', 'report', 'record', 'document',
            'paper', 'file', 'folder', 'book', 'magazine', 'newspaper', 'article', 'page',
            'chapter', 'section', 'paragraph', 'sentence', 'word', 'letter', 'character', 'symbol',
            'number', 'digit', 'figure', 'calculation', 'mathematics', 'science', 'technology', 'computer',
            'internet', 'website', 'email', 'message', 'communication', 'conversation', 'discussion', 'debate',
            'argument', 'fight', 'conflict', 'war', 'peace', 'agreement', 'deal', 'contract',
            'promise', 'commitment', 'decision', 'choice', 'option', 'alternative', 'selection', 'preference',
            'opinion', 'view', 'belief', 'faith', 'religion', 'god', 'church', 'prayer',
            'hope', 'wish', 'dream', 'nightmare', 'reality', 'truth', 'fact', 'lie',
            'secret', 'mystery', 'surprise', 'shock', 'wonder', 'miracle', 'magic', 'spell',
            'curse', 'blessing', 'luck', 'fortune', 'chance', 'fate', 'destiny', 'future',
            'prediction', 'forecast', 'weather', 'climate', 'temperature', 'season', 'spring', 'summer',
            'autumn', 'winter', 'rain', 'snow', 'wind', 'storm', 'thunder', 'lightning',
            'sun', 'moon', 'star', 'planet', 'earth', 'world', 'universe', 'space',
            'nature', 'environment', 'air', 'water', 'fire', 'earth', 'ground', 'soil',
            'rock', 'stone', 'mountain', 'hill', 'valley', 'river', 'lake', 'ocean',
            'sea', 'beach', 'forest', 'tree', 'plant', 'flower', 'grass', 'leaf',
            'animal', 'bird', 'fish', 'dog', 'cat', 'horse', 'cow', 'pig',
            'chicken', 'sheep', 'goat', 'rabbit', 'mouse', 'rat', 'lion', 'tiger',
            'elephant', 'monkey', 'snake', 'spider', 'insect', 'butterfly', 'bee', 'ant',
            'food', 'meal', 'breakfast', 'lunch', 'dinner', 'snack', 'drink', 'water',
            'milk', 'juice', 'coffee', 'tea', 'beer', 'wine', 'alcohol', 'sugar',
            'salt', 'pepper', 'spice', 'herb', 'oil', 'butter', 'cheese', 'meat',
            'beef', 'pork', 'chicken', 'fish', 'egg', 'bread', 'cake', 'cookie',
            'fruit', 'apple', 'orange', 'banana', 'grape', 'strawberry', 'vegetable', 'potato',
            'tomato', 'onion', 'carrot', 'lettuce', 'rice', 'pasta', 'pizza', 'sandwich',
            'soup', 'salad', 'sauce', 'dish', 'plate', 'bowl', 'cup', 'glass',
            'bottle', 'can', 'box', 'bag', 'package', 'container', 'jar', 'pot',
            'pan', 'knife', 'fork', 'spoon', 'tool', 'equipment', 'machine', 'device',
            'instrument', 'apparatus', 'gadget', 'appliance', 'vehicle', 'car', 'truck', 'bus',
            'train', 'plane', 'ship', 'boat', 'bicycle', 'motorcycle', 'wheel', 'engine',
            'motor', 'fuel', 'gas', 'oil', 'electricity', 'energy', 'power', 'battery',
            'wire', 'cable', 'rope', 'chain', 'metal', 'iron', 'steel', 'gold',
            'silver', 'copper', 'plastic', 'rubber', 'glass', 'wood', 'paper', 'cloth',
            'fabric', 'leather', 'cotton', 'wool', 'silk', 'clothes', 'clothing', 'dress',
            'shirt', 'pants', 'skirt', 'coat', 'jacket', 'hat', 'cap', 'shoe',
            'boot', 'sock', 'glove', 'belt', 'watch', 'jewelry', 'ring', 'necklace',
            'money', 'cash', 'coin', 'bill', 'dollar', 'cent', 'bank', 'account',
            'credit', 'debt', 'loan', 'interest', 'investment', 'business', 'trade', 'market',
            'economy', 'industry', 'factory', 'production', 'manufacturing', 'construction', 'building', 'architecture',
            'design', 'art', 'music', 'song', 'dance', 'performance', 'show', 'entertainment',
            'game', 'sport', 'competition', 'match', 'race', 'tournament', 'championship', 'victory',
            'defeat', 'winner', 'loser', 'prize', 'reward', 'gift', 'present', 'surprise',
            'party', 'celebration', 'festival', 'holiday', 'vacation', 'trip', 'journey', 'travel',
            'tour', 'visit', 'adventure', 'experience', 'memory', 'photograph', 'picture', 'image',
            'video', 'film', 'movie', 'television', 'radio', 'newspaper', 'magazine', 'book',
            'library', 'education', 'school', 'university', 'college', 'student', 'teacher', 'professor',
            'lesson', 'class', 'course', 'subject', 'exam', 'test', 'homework', 'assignment',
            'grade', 'mark', 'score', 'result', 'achievement', 'success', 'failure', 'mistake',
            'error', 'problem', 'difficulty', 'challenge', 'obstacle', 'barrier', 'limitation', 'restriction',
            'permission', 'approval', 'acceptance', 'rejection', 'refusal', 'denial', 'confirmation', 'verification',
            'proof', 'evidence', 'witness', 'testimony', 'statement', 'declaration', 'announcement', 'advertisement',
            'publicity', 'promotion', 'campaign', 'marketing', 'sales', 'purchase', 'shopping', 'customer',
            'service', 'quality', 'satisfaction', 'complaint', 'criticism', 'praise', 'compliment', 'thanks',
            'gratitude', 'appreciation', 'respect', 'admiration', 'love', 'affection', 'friendship', 'relationship',
            'marriage', 'wedding', 'divorce', 'separation', 'birth', 'death', 'funeral', 'cemetery',
            'grave', 'spirit', 'soul', 'mind', 'brain', 'thought', 'idea', 'imagination',
            'creativity', 'inspiration', 'motivation', 'encouragement', 'support', 'help', 'assistance', 'aid',
            'rescue', 'salvation', 'protection', 'security', 'safety', 'danger', 'risk', 'threat',
            'attack', 'defense', 'weapon', 'gun', 'knife', 'sword', 'bomb', 'explosion',
            'fire', 'smoke', 'flame', 'heat', 'burn', 'pain', 'suffering', 'agony',
            'torture', 'punishment', 'penalty', 'fine', 'prison', 'jail', 'cell', 'freedom',
            'liberty', 'independence', 'democracy', 'republic', 'monarchy', 'dictatorship', 'tyranny', 'oppression',
            'revolution', 'rebellion', 'protest', 'demonstration', 'strike', 'boycott', 'resistance', 'opposition',
            'enemy', 'opponent', 'rival', 'competitor', 'ally', 'partner', 'colleague', 'teammate',
            'cooperation', 'collaboration', 'partnership', 'alliance', 'union', 'organization', 'institution', 'association',
            'society', 'club', 'group', 'team', 'crew', 'staff', 'personnel', 'workforce',
            'employee', 'worker', 'laborer', 'professional', 'expert', 'specialist', 'consultant', 'advisor',
            'manager', 'director', 'supervisor', 'boss', 'leader', 'chief', 'president', 'chairman',
            'owner', 'founder', 'creator', 'inventor', 'designer', 'architect', 'engineer', 'scientist',
            'researcher', 'scholar', 'academic', 'intellectual', 'philosopher', 'writer', 'author', 'poet',
            'artist', 'painter', 'musician', 'singer', 'actor', 'performer', 'celebrity', 'star',
            'hero', 'champion', 'winner', 'master', 'expert', 'genius', 'talent', 'skill',
            'ability', 'capacity', 'capability', 'potential', 'opportunity', 'chance', 'possibility', 'probability',
            'certainty', 'uncertainty', 'doubt', 'confidence', 'trust', 'faith', 'belief', 'conviction',
            'opinion', 'judgment', 'evaluation', 'assessment', 'analysis', 'examination', 'investigation', 'research',
            'study', 'survey', 'interview', 'questionnaire', 'poll', 'vote', 'election', 'campaign',
            'candidate', 'politician', 'government', 'administration', 'authority', 'power', 'control', 'influence',
            'impact', 'effect', 'consequence', 'result', 'outcome', 'conclusion', 'summary', 'report',
            'account', 'description', 'explanation', 'interpretation', 'translation', 'version', 'edition', 'copy',
            'original', 'duplicate', 'reproduction', 'imitation', 'fake', 'forgery', 'counterfeit', 'fraud',
            'crime', 'offense', 'violation', 'breach', 'infringement', 'trespass', 'invasion', 'intrusion',
            'interference', 'disruption', 'disturbance', 'noise', 'sound', 'voice', 'speech', 'language',
            'word', 'term', 'phrase', 'expression', 'statement', 'sentence', 'paragraph', 'text',
            'document', 'paper', 'file', 'record', 'data', 'information', 'knowledge', 'wisdom',
            'understanding', 'comprehension', 'awareness', 'consciousness', 'recognition', 'realization', 'discovery', 'invention',
            'creation', 'innovation', 'improvement', 'development', 'progress', 'advancement', 'evolution', 'change',
            'transformation', 'conversion', 'adaptation', 'adjustment', 'modification', 'alteration', 'revision', 'correction',
            'fix', 'repair', 'maintenance', 'service', 'care', 'treatment', 'therapy', 'medicine',
            'drug', 'medication', 'pill', 'tablet', 'capsule', 'injection', 'vaccine', 'surgery',
            'operation', 'procedure', 'process', 'method', 'technique', 'approach', 'strategy', 'plan',
            'program', 'project', 'scheme', 'system', 'structure', 'organization', 'arrangement', 'order',
            'sequence', 'series', 'chain', 'link', 'connection', 'relationship', 'association', 'correlation',
            'comparison', 'contrast', 'difference', 'similarity', 'resemblance', 'likeness', 'match', 'fit',
            'suit', 'appropriate', 'suitable', 'proper', 'correct', 'right', 'accurate', 'precise',
            'exact', 'specific', 'particular', 'special', 'unique', 'individual', 'personal', 'private',
            'public', 'general', 'common', 'ordinary', 'normal', 'regular', 'standard', 'typical',
            'average', 'medium', 'moderate', 'reasonable', 'fair', 'just', 'equal', 'balanced',
            'stable', 'steady', 'consistent', 'constant', 'permanent', 'lasting', 'durable', 'strong',
            'solid', 'firm', 'tight', 'secure', 'safe', 'protected', 'covered', 'hidden',
            'secret', 'mysterious', 'unknown', 'unfamiliar', 'strange', 'odd', 'weird', 'unusual',
            'extraordinary', 'remarkable', 'amazing', 'incredible', 'unbelievable', 'impossible', 'difficult', 'hard',
            'tough', 'challenging', 'demanding', 'strict', 'severe', 'harsh', 'cruel', 'brutal',
            'violent', 'aggressive', 'hostile', 'angry', 'furious', 'mad', 'crazy', 'insane',
            'stupid', 'foolish', 'silly', 'ridiculous', 'absurd', 'nonsense', 'meaningless', 'pointless',
            'useless', 'worthless', 'valuable', 'precious', 'expensive', 'costly', 'cheap', 'affordable',
            'reasonable', 'fair', 'unfair', 'unjust', 'wrong', 'evil', 'bad', 'terrible',
            'awful', 'horrible', 'disgusting', 'nasty', 'ugly', 'beautiful', 'pretty', 'attractive',
            'handsome', 'gorgeous', 'lovely', 'nice', 'pleasant', 'enjoyable', 'fun', 'entertaining',
            'interesting', 'exciting', 'thrilling', 'amazing', 'wonderful', 'fantastic', 'great', 'excellent',
            'outstanding', 'perfect', 'ideal', 'best', 'better', 'good', 'fine', 'okay',
            'alright', 'satisfactory', 'adequate', 'sufficient', 'enough', 'plenty', 'lots', 'many',
            'much', 'more', 'most', 'all', 'every', 'each', 'both', 'either',
            'neither', 'none', 'nothing', 'nobody', 'no', 'not', 'never', 'nowhere',
            'yes', 'yeah', 'sure', 'certainly', 'definitely', 'absolutely', 'completely', 'totally',
            'entirely', 'fully', 'quite', 'rather', 'pretty', 'very', 'extremely', 'incredibly',
            'unbelievably', 'surprisingly', 'unexpectedly', 'suddenly', 'immediately', 'instantly', 'quickly', 'rapidly',
            'fast', 'slow', 'slowly', 'carefully', 'gently', 'softly', 'quietly', 'silently',
            'loudly', 'clearly', 'obviously', 'apparently', 'evidently', 'probably', 'possibly', 'maybe',
            'perhaps', 'definitely', 'certainly', 'surely', 'truly', 'really', 'actually', 'indeed',
            'finally', 'eventually', 'ultimately', 'basically', 'essentially', 'fundamentally', 'primarily', 'mainly',
            'mostly', 'generally', 'usually', 'normally', 'typically', 'commonly', 'frequently', 'often',
            'sometimes', 'occasionally', 'rarely', 'seldom', 'hardly', 'barely', 'scarcely', 'almost',
            'nearly', 'about', 'around', 'approximately', 'roughly', 'exactly', 'precisely', 'specifically',
            'particularly', 'especially', 'notably', 'remarkably', 'significantly', 'considerably', 'substantially', 'greatly',
            'highly', 'deeply', 'seriously', 'badly', 'poorly', 'well', 'better', 'best',
            'worse', 'worst', 'less', 'least', 'more', 'most', 'too', 'also',
            'as', 'so', 'such', 'like', 'unlike', 'similar', 'different', 'same',
            'other', 'another', 'else', 'otherwise', 'instead', 'rather', 'quite', 'fairly',
            'pretty', 'somewhat', 'kind', 'sort', 'type', 'way', 'manner', 'style',
            'form', 'shape', 'size', 'length', 'width', 'height', 'depth', 'weight',
            'age', 'date', 'time', 'year', 'month', 'week', 'day', 'hour',
            'minute', 'second', 'morning', 'afternoon', 'evening', 'night', 'today', 'tomorrow',
            'yesterday', 'now', 'then', 'soon', 'late', 'early', 'before', 'after',
            'during', 'while', 'until', 'since', 'from', 'to', 'into', 'onto',
            'upon', 'over', 'under', 'below', 'above', 'behind', 'front', 'back',
            'side', 'top', 'bottom', 'inside', 'outside', 'between', 'among', 'through',
            'across', 'along', 'around', 'near', 'close', 'far', 'away', 'here',
            'there', 'where', 'everywhere', 'anywhere', 'somewhere', 'nowhere', 'left', 'right',
            'straight', 'forward', 'backward', 'up', 'down', 'north', 'south', 'east',
            'west', 'center', 'middle', 'edge', 'corner', 'end', 'beginning', 'start',
            'finish', 'complete', 'whole', 'part', 'piece', 'bit', 'some', 'any',
            'every', 'all', 'none', 'few', 'several', 'many', 'much', 'little',
            'enough', 'plenty', 'lots', 'tons', 'dozens', 'hundreds', 'thousands', 'millions',
            'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
            'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen',
            'sixteen', 'seventeen', 'eighteen', 'nineteen', 'twenty', 'thirty', 'forty', 'fifty',
            'sixty', 'seventy', 'eighty', 'ninety', 'hundred', 'thousand', 'million', 'billion'
        }
        
        # CONTROLLO MIGLIORATO: Solo se il messaggio è tra 2-10 parole E tutte sono inglesi STRICT
        if 2 <= len(words_in_text_lower_no_punct) <= 10:
            english_words_found_in_msg = words_in_text_lower_no_punct.intersection(strict_english_only)
            if len(english_words_found_in_msg) == len(words_in_text_lower_no_punct):
                # CONTROLLO AGGIUNTIVO: Verifica se ci sono parole che potrebbero essere italiane
                # anche se sono nel set inglese (come "no", "ok", etc.)
                ambiguous_words = {'no', 'ok', 'okay', 'stop', 'start', 'post', 'master', 'computer', 'internet', 'email'}
                if words_in_text_lower_no_punct.intersection(ambiguous_words):
                    self.logger.debug(f"✅ Parole ambigue rilevate ({words_in_text_lower_no_punct.intersection(ambiguous_words)}), considerato italiano. PERMESSO: '{clean_text[:100]}...'")
                    return False
                
                self.logger.info(f"❌ Lingua NON CONSENTITA (probabilmente solo Inglese strict: {list(english_words_found_in_msg)}) in '{clean_text[:100]}...'")
                return True
        
        # CONTROLLO 6: Langdetect per testi più lunghi (≥20 caratteri alfabetici)
        if total_alpha_chars_original >= 20:
            detected_lang_code = self.detect_language(clean_text)
            if detected_lang_code:
                lang_mapping = {'italian': 'it', 'it': 'it'}
                allowed_codes = [lang_mapping.get(lang.lower(), lang.lower()) for lang in self.allowed_languages]
                if detected_lang_code not in allowed_codes:
                    # CONTROLLO FALLBACK MIGLIORATO: Verifica presenza italiana
                    italian_words_found_set_for_fallback = words_in_text_lower_no_punct.intersection(italian_indicators)
                    if len(words_in_text_lower_no_punct) > 0:
                        italian_word_ratio_in_fallback = len(italian_words_found_set_for_fallback) / len(words_in_text_lower_no_punct)
                        # SOGLIA RIDOTTA: 15% invece di 20% per essere meno aggressivi
                        if italian_word_ratio_in_fallback >= 0.15 or \
                        (len(italian_words_found_set_for_fallback) >= 2 and len(words_in_text_lower_no_punct) < 10):
                            self.logger.info(f"✅ Langdetect ha rilevato '{detected_lang_code}', ma presenza italiana significativa ({italian_word_ratio_in_fallback:.2%}, parole: {list(italian_words_found_set_for_fallback)}) sovrascrive. PERMESSO: '{clean_text[:100]}...'")
                            return False
                    
                    self.logger.info(f"❌ Lingua NON CONSENTITA (Langdetect: '{detected_lang_code}', consentite: {allowed_codes}) per '{clean_text[:100]}...'")
                    return True
        else:
            self.logger.debug(f"✅ Testo troppo breve per Langdetect ({total_alpha_chars_original} caratteri alfabetici < 20). PERMESSO (default).")
        
        # Default: permetti
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
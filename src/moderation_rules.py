import logging
import os
import re
import functools
from typing import List, Dict, Any, Optional, Tuple

import unidecode
from openai import OpenAI, OpenAIError

from .config_manager import ConfigManager
# from .sheets_interface import GoogleSheetsManager # Non direttamente usato qui, ma passato al costruttore
from .cache_utils import MessageAnalysisCache

try:
    import langdetect
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False
    logging.getLogger(__name__).warning("Libreria langdetect non trovata. Il rilevamento della lingua sarà limitato.")


class AdvancedModerationBotLogic: # Rinominato per chiarezza rispetto a TelegramModerationBot
    """
    Contiene la logica di analisi e moderazione dei messaggi,
    inclusa la normalizzazione del testo, il controllo delle parole bannate,
    l'analisi della lingua e l'interazione con OpenAI.
    """
    def __init__(self, config_manager: ConfigManager, logger: logging.Logger): # Rimossa dipendenza da SheetsManager
        self.config_manager = config_manager
        self.logger = logger
        
        self.banned_words: List[str] = self.config_manager.get('banned_words', [])
        # NUOVA: Carica whitelist
        self.whitelist_words: List[str] = self.config_manager.get('whitelist_words', [])
        self.allowed_languages: List[str] = self.config_manager.get('allowed_languages', ["italian"])
        
        # Log della configurazione whitelist
        self.logger.info(f"Whitelist caricata con {len(self.whitelist_words)} parole: {self.whitelist_words}")
        
        self.char_map: Dict[str, str] = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"}
        self.analysis_cache = MessageAnalysisCache(cache_size=1000)
        
        self.stats: Dict[str, Any] = {
            'total_messages_analyzed_by_openai': 0, # Rinominato per chiarezza
            'direct_filter_matches': 0, # Rinominato
            'ai_filter_violations': 0,  # Rinominato
            'openai_requests': 0,
            'openai_cache_hits': 0,
            # Le statistiche 'deleted_messages', 'edited_messages_detected', 'edited_messages_deleted'
            # sono più appropriate nella classe bot principale che gestisce le azioni Telegram.
        }
        
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            self.openai_client = OpenAI(api_key=api_key)
            self.logger.info("Client OpenAI inizializzato.")
        else:
            self.openai_client = None
            self.logger.warning("OPENAI_API_KEY non trovato. L'analisi AI non sarà disponibile.")

    @functools.lru_cache(maxsize=200)
    def contains_whitelist_word(self, text: str) -> bool:
        """
        Verifica se il messaggio contiene parole della whitelist.
        Se contiene almeno una parola della whitelist, il messaggio viene auto-approvato.
        """
        if not self.whitelist_words:
            return False
        
        # Normalizza il testo per il confronto
        normalized_text = self.normalize_text(text)
        if not normalized_text:
            return False
        
        # Verifica se qualsiasi parola della whitelist è presente
        for whitelist_word in self.whitelist_words:
            normalized_whitelist_word = whitelist_word.lower().strip()
            if normalized_whitelist_word in normalized_text:
                self.logger.debug(f"Whitelist match: '{whitelist_word}' trovata in '{text[:50]}...'")
                return True
        
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Restituisce le statistiche di analisi dei messaggi."""
        total_analyzed = self.stats['total_messages_analyzed_by_openai']
        cache_hits = self.stats['openai_cache_hits']
        
        return {
            **self.stats,
            'cache_size': len(self.analysis_cache.cache),
            'cache_hit_rate': (cache_hits / total_analyzed) if total_analyzed > 0 else 0,
        }

    def normalize_text(self, text: str) -> str:
        """
        Normalizza il testo per migliorare il rilevamento:
        rimuove accenti, markdown, emoji, caratteri speciali (tranne @),
        sostituisce numeri con lettere simili, e converte in minuscolo.
        """
        # Rimuovi formattazione Markdown comune
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)  # Grassetto
        text = re.sub(r'__(.*?)__', r'\1', text)      # Corsivo/Sottolineato
        text = re.sub(r'~~(.*?)~~', r'\1', text)      # Barrato
        text = re.sub(r'`(.*?)`', r'\1', text)        # Codice inline
        text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text) # Link [testo](url) -> testo

        # Rimuovi emoji e simboli specifici (lista estesa)
        emoji_pattern = re.compile(
            "["
            u"\U0001F600-\U0001F64F"  # emoticons
            u"\U0001F300-\U0001F5FF"  # symbols & pictographs
            u"\U0001F680-\U0001F6FF"  # transport & map symbols
            u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
            u"\U00002702-\U000027B0"  # Dingbats
            u"\U000024C2-\U0001F251" 
            u"\u2600-\u26FF"          # Simboli vari
            u"\u2700-\u27BF"          # Dingbats
            u"🔴🔵⚪⚫🟠🟡🟢🟣⚽⚾🥎🏀🏐🏈🏉🎱🪀🏓⚠️🚨🚫⛔️🆘🔔🔊📢📣" # Simboli specifici
            "]+", flags=re.UNICODE)
        text = emoji_pattern.sub('', text)

        text = unidecode.unidecode(text.lower()) # Minuscolo e rimuovi accenti
        text = re.sub(r"[^a-z0-9\s@]", "", text) # Rimuovi non alfanumerici (mantieni spazi e @)

        for char_from, char_to in self.char_map.items(): # Sostituisci numeri con lettere
            text = text.replace(char_from, char_to)
        
        text = re.sub(r'\s+', ' ', text).strip() # Normalizza spazi
        return text

    @functools.lru_cache(maxsize=500)
    def contains_banned_word(self, text: str) -> bool:
        """
        Filtro meccanico MIGLIORATO - blocca anche spam mascherato.
        CORREZIONE: Migliorato per rilevare correttamente le parole bannate.
        """
        if not text or not text.strip():
            return False
        
        # DEBUG: Log del testo originale
        self.logger.debug(f"Filtro diretto - Testo originale: '{text}'")
        
        # ===== CONTROLLO 1: PAROLE BANNATE ESATTE (PRIMA DELLA NORMALIZZAZIONE) =====
        text_lower = text.lower()
        
        # Controllo diretto su parole bannate dalla configurazione
        for banned_word in self.banned_words:
            banned_word_lower = banned_word.lower().strip()
            if banned_word_lower in text_lower:
                self.logger.info(f"MATCH filtro diretto: parola bannata '{banned_word}' trovata in '{text[:50]}...'")
                return True
        
        # ===== CONTROLLO 2: LINK TELEGRAM ESTERNI + MATERIALE =====
        # Pattern per rilevare link telegram con pattern di vendita/offerta materiale
        telegram_link_patterns = [
            r'(?:https?://)?(?:t\.me|telegram\.me)/\w+',  # Link t.me/canale
            r'@\w+',  # Username telegram
        ]
        
        # Parole che indicano offerta/vendita di materiale didattico
        material_offer_words = [
            'panieri', 'riassunti', 'appunti', 'materiale', 'slides', 'dispense',
            'tesi', 'esami', 'soluzioni', 'quiz', 'test', 'simulazioni'
        ]
        
        # Parole che indicano invito/promozione
        invitation_words = [
            'iscriversi', 'iscrivetevi', 'entrate', 'joinare', 'accedere', 'accesso',
            'canale', 'gruppo', 'link', 'qui', 'sotto', 'sopra', 'clicca', 'segui'
        ]
        
        # Se contiene link telegram + materiale + invito = SPAM
        has_telegram_link = any(re.search(pattern, text_lower, re.IGNORECASE) for pattern in telegram_link_patterns)
        has_material_offer = any(word in text_lower for word in material_offer_words)
        has_invitation = any(word in text_lower for word in invitation_words)
        
        if has_telegram_link and has_material_offer and has_invitation:
            self.logger.info(f"MATCH filtro diretto: link Telegram + offerta materiale + invito in '{text[:50]}...'")
            return True
        
        # ===== CONTROLLO 3: ALFABETI NON-LATINI =====
        normalized_text = self.normalize_text(text)
        if not normalized_text:
            return False
        
        self.logger.debug(f"Filtro diretto - Testo normalizzato: '{normalized_text}'")

        # Test rapido per caratteri cirillici PRIMA della normalizzazione
        cyrillic_count = sum(1 for char in text if '\u0400' <= char <= '\u04FF' or '\u0500' <= char <= '\u052F')
        if cyrillic_count >= 3:  # Se ci sono almeno 3 caratteri cirillici
            self.logger.info(f"MATCH filtro diretto: {cyrillic_count} caratteri cirillici in '{text[:50]}...'")
            return True

        # ===== CONTROLLO 4: PATTERN SPAM MASCHERATO =====
        masked_panieri_spam_patterns = [
            # "Chi cerca panieri..." + invito contatto
            r"chi\s+cerc[ao]\s+panier.*(?:scriv|contatt|privat)",
            r"cerc[ao]\s+panier.*(?:scriv|contatt|privat)",
            
            # "Ho materiale/panieri..." + invito contatto  
            r"ho\s+(?:material|panier).*(?:scriv|contatt|privat)",
            r"(?:material|panier).*(?:complet|aggiornat).*(?:scriv|contatt|privat)",
            
            # "Panieri disponibili..." + contatto
            r"panier.*disponibil.*(?:scriv|contatt|privat|interessat)",
            r"panier.*(?:2024|2025|aggiornat).*(?:scriv|contatt|privat|interessat)",
            
            # Pattern generici invito contatto per materiale
            r"(?:scriv|contatt).*(?:per|sui)\s+panier",
            r"panier.*(?:scriv|contatt).*(?:privat|dm)",
            r"material.*(?:scriv|contatt).*(?:privat|dm)",
            
            # Formule tipiche spam mascherato
            r"interessat.*(?:scriv|contatt)",
            r"(?:scriv|contatt).*(?:per|chi)\s+(?:material|panier|appunt)",
            r"(?:material|panier).*(?:chi|per).*(?:scriv|contatt)",
            
            # NUOVO: Pattern per vendita/offerta con link esterni
            r"vendita.*(?:panier|riassunt|material).*(?:t\.me|telegram|canale)",
            r"(?:panier|riassunt|material).*vendita.*(?:t\.me|telegram|canale)",
            r"affidatevi.*(?:unico|solo).*canale.*(?:panier|riassunt|material)",
            r"canale.*(?:ufficiale|preposto).*(?:vendita|offerta).*(?:panier|riassunt)",
            
            # Account spam noti (esistenti)
            r"@panieriunipegasomercatorum",
            r"@unitelematica",
        ]

        for pattern in masked_panieri_spam_patterns:
            if re.search(pattern, normalized_text, re.IGNORECASE):
                self.logger.info(f"MATCH filtro diretto (spam mascherato): pattern '{pattern}' in '{text}'")
                return True

        # ===== CONTROLLO 5: SPAM OVVIO =====
        obvious_spam_patterns = [
            # Vendita esplicita con prezzo E contatto privato
            r"(?:vendo|offro).*[0-9]+\s*(?:euro|€).*(?:scriv|contatt|privat|whatsapp|telegram)",
            
            # Scam ovvi
            r"guadagni?\s+(?:facili|garantiti|sicuri)",
            r"(?:soldi|euro)\s+facili",
            r"mining.*pool.*(?:join|entra)",
            
            # Pattern cirillico (dopo normalizzazione, potrebbero essere translitterati)
            r"zarabotok",  # заработок translitterato
            r"rabota",     # работа translitterato  
            r"pishi",      # пиши translitterato
            r"kontakt",    # контакт translitterato
        ]

        for pattern in obvious_spam_patterns:
            if re.search(pattern, normalized_text, re.IGNORECASE):
                self.logger.info(f"MATCH filtro diretto: pattern '{pattern}' in '{normalized_text}'")
                return True

        return False

    def contains_suspicious_contact_invitation(self, text: str) -> bool:
        """
        Verifica se il testo contiene inviti a contatto privato potenzialmente sospetti,
        specialmente se abbinati a termini di vendita o offerta di materiale.
        """
        normalized_text = self.normalize_text(text)
        if not normalized_text: return False

        # Whitelist di contesti legittimi per contatti (es. gruppi di studio)
        # Questi pattern, se presenti, potrebbero rendere l'invito al contatto meno sospetto
        legitimate_contexts = [
            "grupp\w+\s+(?:studio|whatsapp|telegram)", "aggiung\w+\s+gruppo",
            "link\s+gruppo", "mandat\w+\s+numer\w+", "entrare\s+nel\s+gruppo"
        ]
        for legit_pattern in legitimate_contexts:
            if re.search(legit_pattern, normalized_text):
                # Se c'è un contesto legittimo E NON ci sono termini di vendita espliciti,
                # allora l'invito potrebbe essere OK.
                if not any(term in normalized_text for term in ["vendo", "offro", "prezzo", "pagamento", "€", "euro"]):
                    self.logger.debug(f"Invito al contatto in contesto legittimo: '{normalized_text}'")
                    return False # Meno probabile che sia spam commerciale

        # Parole chiave per canali di contatto
        contact_channels = ["whatsapp", "telegram", "instagram", "dm", "direct", "privato", "@\w+"] # @username
        # Parole chiave per azioni di contatto
        contact_actions = ["scriv\w+", "contatt\w+", "mand\w+", "invia\w+", "messaggi\w+"]
        # Materiali/Servizi spesso offerti in modo sospetto
        offered_items = [
            "panier\w+", "appunt\w+", "material\w+", "tesi", "esami", "soluzion\w+",
            "aiuto", "lezioni", "slides", "aggiornat\w+"
        ]

        # Verifica combinazioni sospette
        has_contact_channel = any(re.search(channel, normalized_text) for channel in contact_channels)
        has_contact_action = any(action in normalized_text for action in contact_actions)
        has_offered_item = any(item in normalized_text for item in offered_items)

        if (has_contact_channel or has_contact_action) and has_offered_item:
            self.logger.debug(f"Rilevato invito al contatto sospetto: '{normalized_text}'")
            return True
        
        # Ricerca di @username seguito da offerta
        if re.search(r"@\w+", normalized_text) and has_offered_item:
            self.logger.debug(f"Rilevato @username con offerta materiale: '{normalized_text}'")
            return True

        return False
        
    def detect_language(self, text: str) -> Optional[str]:
        """Rileva la lingua principale del messaggio usando langdetect."""
        if not LANGDETECT_AVAILABLE or not text or len(text.strip()) < 5: # Ignora testi troppo corti
            return None 
        try:
            # Langdetect può lanciare eccezioni su testi molto corti o ambigui
            # Forza la rilevazione di una sola lingua
            detected_langs = langdetect.detect_langs(text)
            if detected_langs:
                return detected_langs[0].lang # Prendi la lingua più probabile
            return None
        except langdetect.lang_detect_exception.LangDetectException:
            self.logger.warning(f"Langdetect non è riuscito a rilevare la lingua per: '{text[:50]}...'")
            return None # Fallback a non rilevato

    def is_language_disallowed(self, text: str) -> bool:
        """
        Determina se la lingua del messaggio non è tra quelle consentite.
        LOGICA MIGLIORATA per ridurre drasticamente i falsi positivi su italiano.
        """
        if not self.allowed_languages or "any" in self.allowed_languages:
            return False

        clean_text = text.strip()
        if not clean_text:
            return False

        self.logger.debug(f"🔍 Analisi lingua per: '{clean_text}'")

        # ===== CONTROLLO 1: ALFABETI CHIARAMENTE NON-LATINI (PRIORITÀ MASSIMA) =====
        cyrillic_chars = sum(1 for char in clean_text if '\u0400' <= char <= '\u04FF' or '\u0500' <= char <= '\u052F')
        arabic_chars = sum(1 for char in clean_text if '\u0600' <= char <= '\u06FF') 
        chinese_chars = sum(1 for char in clean_text if '\u4e00' <= char <= '\u9fff')
        
        total_alpha_chars = len([c for c in clean_text if c.isalpha()])
        
        if total_alpha_chars > 0:
            non_latin_ratio = (cyrillic_chars + arabic_chars + chinese_chars) / total_alpha_chars
            if non_latin_ratio > 0.4:  # Soglia aumentata da 0.3 a 0.4
                self.logger.info(f"❌ Lingua non consentita: {non_latin_ratio:.2%} caratteri non-latini in '{clean_text[:50]}...'")
                return True

        # ===== CONTROLLO 2: WHITELIST ITALIANA ESTESA (NUOVO) =====
        # Lista molto più ampia di parole/pattern chiaramente italiani
        italian_indicators = {
            # Parole comuni italiane
            'ciao', 'buongiorno', 'buonasera', 'buonanotte', 'salve', 'arrivederci',
            'grazie', 'prego', 'scusa', 'scusate', 'perfetto', 'bene', 'male', 'così',
            'si', 'sì', 'no', 'ok', 'okay', 'va', 'sono', 'ho', 'hai', 'ha', 'abbiamo',
            'oggi', 'ieri', 'domani', 'quando', 'dove', 'come', 'cosa', 'chi', 'perché',
            'questo', 'quello', 'questi', 'quelli', 'che', 'con', 'per', 'del', 'della',
            'non', 'più', 'molto', 'poco', 'tutto', 'niente', 'anche', 'ancora', 'già',
            
            # Parole universitarie/contesto
            'università', 'esame', 'esami', 'professore', 'prof', 'crediti', 'corso', 'corsi',
            'laurea', 'triennale', 'magistrale', 'dottorato', 'facoltà', 'appunti', 
            'panieri', 'lezioni', 'tesi', 'sessione', 'matricola', 'ateneo', 'dipartimento',
            'cattedra', 'semestre', 'frequenza', 'iscrizione', 'slides', 'slide',
            
            # Espressioni colloquiali italiane (NUOVO)
            'boh', 'mah', 'beh', 'allora', 'quindi', 'però', 'infatti', 'comunque',
            'speriamo', 'magari', 'forse', 'davvero', 'veramente', 'sicuramente',
            'attendiamo', 'aspettiamo', 'vediamo', 'diciamo', 'facciamo', 'andiamo',
            
            # Forme verbali comuni (NUOVO) 
            'è', 'sono', 'siamo', 'sarà', 'sarebbe', 'potrebbe', 'dovrebbe', 'farebbe',
            'riuscite', 'riesco', 'riesci', 'posso', 'puoi', 'può', 'possiamo', 'potete',
            'voglio', 'vuoi', 'vuole', 'vogliamo', 'volete', 'vogliono',
            
            # Termini tecnici italianizzati (NUOVO)
            'link', 'meet', 'zoom', 'teams', 'chat', 'gruppo', 'canale', 'messaggio',
            'whatsapp', 'telegram', 'email', 'file', 'pdf', 'video', 'audio'
        }
        
        # Converti testo in parole per analisi
        words_in_text = set(word.lower().strip('.,!?;:()[]{}') for word in clean_text.lower().split())
        
        # Se almeno UNA parola è chiaramente italiana, consideriamo il testo italiano
        italian_words_found = words_in_text.intersection(italian_indicators)
        if italian_words_found:
            self.logger.debug(f"✅ Italiano confermato da parole: {list(italian_words_found)} in '{clean_text}'")
            return False
        
        # ===== CONTROLLO 3: PATTERN ITALIANI (MORFOLOGIA) =====
        # Riconosce pattern tipici italiani anche su parole non in whitelist
        italian_patterns = [
            r'\b\w+mente\b',      # avverbi: certamente, probabilmente, etc
            r'\b\w+zione\b',      # sostantivi: informazione, situazione, etc  
            r'\b\w+zioni\b',      # plurali: informazioni, situazioni, etc
            r'\b\w+aggio\b',      # sostantivi: passaggio, messaggio, etc
            r'\b\w+aggio\b',      # sostantivi: passaggio, messaggio, etc
            r'\b\w+are\b',        # infiniti: andare, fare, etc (limitato per evitare false positive)
            r'\b\w+amo\b',        # prima persona plurale: andiamo, facciamo, etc
            r'\b\w+ete\b',        # seconda persona plurale: andate, fate, etc  
            r'\b\w+ano\b',        # terza persona plurale: vanno, fanno, etc
            r'\b\w+oso\b',        # aggettivi: famoso, pericoloso, etc
            r'\b\w+osa\b',        # aggettivi femminili: famosa, pericolosa, etc
            r'\b\w+osi\b',        # aggettivi plurali maschili
            r'\b\w+ose\b',        # aggettivi plurali femminili
        ]
        
        import re
        for pattern in italian_patterns:
            if re.search(pattern, clean_text.lower(), re.IGNORECASE):
                matches = re.findall(pattern, clean_text.lower(), re.IGNORECASE)
                self.logger.debug(f"✅ Italiano confermato da pattern '{pattern}': {matches} in '{clean_text}'")
                return False

        # ===== CONTROLLO 4: ENGLISH-ONLY BLOCKING (MIGLIORATO) =====
        # Solo se il testo è INTERAMENTE in inglese senza mescolanza italiana
        common_english_only = {
            'hello', 'hi', 'hey', 'how', 'are', 'you', 'what', 'where', 'when', 
            'why', 'who', 'can', 'could', 'would', 'should', 'will', 'shall',
            'good', 'bad', 'nice', 'great', 'thank', 'thanks', 'please', 'sorry',
            'yes', 'no', 'okay', 'welcome', 'bye', 'goodbye', 'see', 'get', 'go',
            'the', 'and', 'but', 'for', 'with', 'this', 'that', 'there', 'here',
            'have', 'has', 'had', 'was', 'were', 'been', 'being', 'make', 'made',
            'take', 'took', 'come', 'came', 'went', 'going', 'do', 'does', 'did'
        }
        
        if len(words_in_text) >= 2:  # Solo per testi con almeno 2 parole
            english_words_found = words_in_text.intersection(common_english_only)
            english_ratio = len(english_words_found) / len(words_in_text)
            
            # BLOCCA solo se è MOLTO probabilmente solo inglese (soglia alta)
            if english_ratio >= 0.8 and len(english_words_found) >= 2:
                self.logger.info(f"❌ Messaggio probabilmente inglese: {english_ratio:.2%} parole inglesi ({list(english_words_found)}) in '{clean_text}'")
                return True

        # ===== CONTROLLO 5: LANGDETECT (SOLO PER TESTI LUNGHI, CON FALLBACK MAGGIORE) =====
        # Aumentiamo la soglia a 15 caratteri alfabetici per ridurre errori su testi brevi
        if total_alpha_chars >= 15:  # Soglia aumentata da 8 a 15
            detected_lang_code = self.detect_language(clean_text)
            if detected_lang_code:
                # Mappa le lingue consentite
                lang_mapping = {'italian': 'it', 'it': 'it'}
                allowed_codes = []
                for lang in self.allowed_languages:
                    mapped = lang_mapping.get(lang.lower(), lang.lower())
                    allowed_codes.append(mapped)
                
                if detected_lang_code not in allowed_codes:
                    # ===== FALLBACK AVANZATO: CONTROLLO PRESENZA ITALIANA =====
                    # Se langdetect dice non-italiano ma ci sono chiari segnali italiani, ignoriamo langdetect
                    
                    # Conta parole italiane vs non-italiane
                    total_words = len(words_in_text)
                    italian_word_count = len(words_in_text.intersection(italian_indicators))
                    
                    # Se almeno il 20% delle parole sono italiane, consideriamo il testo italiano
                    if total_words > 0 and (italian_word_count / total_words) >= 0.2:
                        self.logger.info(f"✅ Langdetect dice '{detected_lang_code}' ma {italian_word_count}/{total_words} parole italiane trovate - PERMESSO: '{clean_text}'")
                        return False
                    
                    # Controllo aggiuntivo: se contiene mix di termini tecnici + italiano
                    tech_terms = {'link', 'meet', 'zoom', 'teams', 'gmeet', 'file', 'pdf', 'chat', 'gruppo', 'email'}
                    tech_found = words_in_text.intersection(tech_terms)
                    if tech_found and italian_word_count > 0:
                        self.logger.info(f"✅ Langdetect dice '{detected_lang_code}' ma mix tecnico+italiano rilevato ({tech_found}, {italian_word_count} parole italiane) - PERMESSO: '{clean_text}'")
                        return False
                    
                    # Solo qui blocchiamo davvero
                    self.logger.info(f"❌ Lingua non consentita rilevata da Langdetect: '{detected_lang_code}' (consentite: {allowed_codes}) per '{clean_text[:50]}...'")
                    return True
        else:
            self.logger.debug(f"✅ Testo troppo breve per Langdetect ({total_alpha_chars} caratteri alfabetici < 15) - PERMESSO: '{clean_text}'")

        # ===== DEFAULT: PERMETTI =====
        # Se arriviamo qui, in caso di dubbio permettiamo il messaggio
        self.logger.debug(f"✅ Nessun blocco lingua applicato - PERMESSO: '{clean_text}'")
        return False

    def analyze_with_openai(self, message_text: str) -> Tuple[bool, bool, bool]:
        """
        Analizza il messaggio con OpenAI per determinare se è inappropriato,
        una domanda, o in una lingua non consentita.

        Restituisce: (is_inappropriate, is_question, is_disallowed_language)
        """
        if not self.openai_client:
            self.logger.warning("OpenAI client non disponibile. Analisi AI saltata.")
            # Fallback a controlli locali più semplici
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            is_disallowed_lang_local = self.is_language_disallowed(message_text)
            return is_inappropriate_local, False, is_disallowed_lang_local # Non possiamo determinare "domanda" localmente

        # Messaggi molto corti o solo simboli potrebbero non necessitare di analisi AI costosa
        if len(message_text.strip()) <= 10 or re.match(r'^[^\w\s]+$', message_text.strip()):
            # Potremmo comunque fare un controllo di lingua se LANGDETECT_AVAILABLE
            is_disallowed_lang_short = self.is_language_disallowed(message_text)
            return False, False, is_disallowed_lang_short

        self.stats['total_messages_analyzed_by_openai'] += 1
        
        cached_result = self.analysis_cache.get(message_text)
        if cached_result:
            self.stats['openai_cache_hits'] += 1
            self.logger.debug("Risultato analisi OpenAI dalla cache.")
            return cached_result

        self.stats['openai_requests'] += 1
        
        # Il tuo prompt è molto dettagliato e ben strutturato. Lo manteniamo.
        # IMPORTANTE: Questo prompt è molto lungo, verifica i costi e la latenza con OpenAI.
        # Potrebbe essere necessario ottimizzarlo o usare modelli più economici/veloci per alcuni task.
        system_prompt = (
            "Sei un moderatore esperto di un gruppo Telegram universitario italiano. Analizza ogni messaggio con attenzione e rispondi SOLO con questo formato:\n"
            "INAPPROPRIATO: SI/NO\n"
            "DOMANDA: SI/NO\n"
            "LINGUA: CONSENTITA/NON CONSENTITA\n\n"
            
            "⚠️ PRIORITÀ ASSOLUTA: EVITARE FALSI POSITIVI! CONSIDERA APPROPRIATO QUALSIASI MESSAGGIO CHE NON È CHIARAMENTE PROBLEMATICO.\n\n"

            "REGOLE PER ALFABETI NON LATINI:\n"
            "❌ Qualsiasi messaggio che contiene prevalentemente testo in cirillico o altri alfabeti non latini deve essere marcato come LINGUA: NON CONSENTITA.\n"
            "❌ Messaggi con @username seguiti da testo in cirillico sono sempre da considerare INAPPROPRIATO: SI\n"
            "❌ Messaggi con emoji + testo in cirillico sono sempre da considerare INAPPROPRIATO: SI\n"
            "❌ Annunci pubblicitari in qualsiasi lingua diversa dall'italiano sono sempre INAPPROPRIATO: SI\n\n"
            
            "PROCESSO DI ANALISI (da seguire in ordine):\n"
            "1. Verifica se il messaggio è completamente in lingua straniera\n"
            "2. Verifica se contiene insulti gravi diretti ad altri utenti\n"
            "3. Verifica se contiene offerte commerciali ESPLICITE con menzione di pagamenti\n"
            "4. Verifica se contiene promozioni di investimenti, trading o criptovalute\n"
            "5. **NUOVO**: Verifica se contiene link a canali esterni per vendita/offerta materiale didattico\n"
            "6. Verifica se il messaggio è una DOMANDA (con o senza punto interrogativo)\n"
            "7. Se hai dubbi, considera il messaggio APPROPRIATO\n\n"
            
            "DETTAGLIO DEI CRITERI:\n\n"
            
            "1️⃣ LINGUA (analizza per prima cosa):\n"
            f"Lingue consentite (codici ISO 639-1): {self.allowed_languages}\n"
            "❌ NON CONSENTITA: SOLO messaggi INTERAMENTE in lingua straniera (non tra quelle consentite) senza italiano\n"
            "    • Esempio non consentito (se solo 'it' è consentito): Hello everyone how are you today\n"
            "✅ CONSENTITA: Tutto il resto, incluso:\n"
            "    • Messaggi in italiano con alcune parole straniere\n"
            "    • Messaggi che citano o discutono lingue straniere\n"
            "    • Messaggi che contengono termini tecnici in inglese\n"
            "    • Messaggi con errori grammaticali o sintattici\n\n"
            
            "2️⃣ INAPPROPRIATO (solo questi casi specifici sono inappropriati):\n"
            "❌ Vendita ESPLICITA di materiale didattico con CHIARA menzione di denaro\n"
            "    • Vendo panieri a 20€, Offro appunti a pagamento, Materiale disponibile a 15€\n"
            "❌ Transazioni commerciali con termini ESPLICITI come:\n"
            "    • prezzo, costo, euro, €, pagamento, acquistare, vendere, comprare, soldi\n"
            "❌ Inviti a contattare privatamente SOLO SE accompagnati da termini commerciali:\n"
            "    • Scrivetemi in privato per acquistare, Contattatemi per prezzi\n"
            "❌ **NUOVO CRITICO**: Link a canali esterni Telegram per vendita/offerta materiale didattico:\n"
            "    • Qualsiasi messaggio che contiene link t.me/canale + offerta di panieri/riassunti/materiale\n"
            "    • Messaggi che promuovono 'canali ufficiali' per vendita materiale didattico\n"
            "    • Inviti a iscriversi a canali esterni per ottenere materiale didattico\n"
            "    • Esempi: 'Iscrivetevi al canale t.me/panieri', 'Materiale disponibile su t.me/riassunti'\n"
            "    • 'Affidatevi all'unico canale preposto alla vendita di panieri'\n"
            "❌ Insulti pesanti diretti ad altri utenti:\n"
            "    • Offese personali gravi, linguaggio d'odio, minacce\n"
            "❌ Promozioni di investimenti o trading:\n"
            "    • Messaggi che promuovono guadagni facili attraverso trading o investimenti\n"
            "    • Messaggi che promuovono esperti di trading/investimenti da contattare\n"
            "    • Promozioni di servizi di consulenza per investimenti o trading\n"
            "    • Offerte di guadagno attraverso criptovalute o forex\n"
            "    • Messaggi che condividono link a gruppi o bot per investimenti\n\n"
            
            "ATTENZIONE SPAM MASCHERATO DI PANIERI (SEMPRE INAPPROPRIATO):\n"
            "❌ Qualsiasi messaggio che invita al contatto privato per panieri/materiale È SEMPRE INAPPROPRIATO, anche senza menzione di prezzo:\n"
            "    • Ciao, chi cerca panieri aggiornati mi scriva\n"
            "    • Ho materiale completo, contattatemi\n" 
            "    • Panieri 2024 disponibili, interessati in privato\n"
            "    • Chi vuole i panieri mi contatti\n"
            "    • Ho tutto il materiale, scrivetemi\n"
            "    • Panieri completi, contattatemi per info\n"
            "❌ REGOLA: Se qualcuno offre panieri/materiale E chiede di essere contattato privatamente = INAPPROPRIATO: SI\n"
            "❌ Anche frasi come 'mi scriva', 'contattatemi', 'interessati in privato' sono SEMPRE sospette se legate a panieri\n\n"
            
            "❌ **NUOVA REGOLA CRITICA - LINK A CANALI ESTERNI**:\n"
            "❌ Qualsiasi messaggio che contiene link a canali Telegram esterni (t.me/*, telegram.me/*) combinato con:\n"
            "    • Offerta di materiale didattico (panieri, riassunti, appunti, slides, etc.)\n"
            "    • Inviti a iscriversi per ottenere materiale\n"
            "    • Promozione di 'canali ufficiali' per materiale\n"
            "    • È SEMPRE INAPPROPRIATO: SI, anche se non menziona prezzi esplicitamente\n"
            "❌ Esempi SEMPRE inappropriati:\n"
            "    • 'Iscrivetevi al canale https://t.me/panieri per materiale aggiornato'\n"
            "    • 'Affidatevi all'unico canale ufficiale preposto alla vendita di panieri t.me/riassunti'\n"
            "    • 'Qui sotto il link del canale dove iscriversi se volete panieri https://t.me/materiale'\n"
            "    • Qualsiasi variazione che combina link esterni + materiale didattico\n\n"
            
            "3️⃣ CASI SEMPRE APPROPRIATI (non marcare mai come inappropriati):\n"
            "✅ Richieste di materiale didattico tra studenti:\n"
            "    • Qualcuno ha i panieri di questo esame?, Avete gli appunti per Diritto Privato?\n"
            "✅ Richieste di aggiunta a gruppi di studio o scambio numeri per gruppi:\n"
            "    • Mandatemi i vostri numeri per il gruppo WhatsApp, Posso entrare nel gruppo di studio?\n"
            "✅ Scambio di contatti per GRUPPI DI STUDIO (mai marcare come inappropriato):\n"
            "    • Scrivetemi in privato per entrare nel gruppo, Vi aggiungo al gruppo WhatsApp\n"
            "✅ Discussioni accademiche legittime su economia, finanza o criptovalute\n"
            "✅ Lamentele sull'università o sui servizi didattici\n"
            "✅ Domande su esami, procedure burocratiche, certificati, date\n"
            "✅ Messaggi brevi, emoji, numeri di telefono, indirizzi email\n\n"
            "✅ Richieste di compilazione questionari o partecipazione a ricerche accademiche:\n"
            "    • Studenti che cercano partecipanti per tesi, ricerche o progetti universitari\n"
            "    • Link a Google Forms, SurveyMonkey, o altre piattaforme di sondaggi per scopi didattici\n"
            "    • Richieste di aiuto per raccolta dati o partecipazione a esperimenti accademici\n"
            "    • Link relativi a contenuti didattici o universitari come progetti di ricerca legittimi\n\n"
            "✅ Richieste legittime di panieri che NON sono offerte di vendita:\n"
            "    • Ciao a tutti, qualcuno ha i panieri aggiornati di diritto privato?\n"
            "    • Cerco i panieri aggiornati, qualcuno può aiutarmi?\n\n"

            "\nREGOLE SPECIALI PER LINK:\n"
            "✅ Link a gruppi WhatsApp sono SEMPRE appropriati se:\n"
            "    • Non contengono menzione esplicita di denaro o pagamenti\n"
            "    • Non promuovono vendita di materiale didattico\n"
            "    • Non contengono termini commerciali (prezzo, costo, euro, €, pagamento)\n"
            "❌ Link a canali Telegram esterni (t.me/*, telegram.me/*) sono INAPPROPRIATI se:\n"
            "    • Combinati con offerta di materiale didattico\n"
            "    • Presentati come 'canali ufficiali' per materiale\n"
            "    • Contengono inviti a iscriversi per ottenere panieri/riassunti/materiale\n"
            "\n⚠️ Esempi di link SEMPRE APPROPRIATI:\n"
            "    • https://chat.whatsapp.com/Ab12CdEfGhIj (semplice link senza testo aggiuntivo)\n"
            "    • Ecco il link: https://chat.whatsapp.com/Ab12CdEfGhIj\n"
            "⚠️ Esempi di link SEMPRE INAPPROPRIATI:\n"
            "    • https://t.me/panieri + qualsiasi menzione di materiale didattico\n"
            "    • t.me/riassunti + invito a iscriversi per materiale\n\n"
            
            "ESEMPI DI MESSAGGI CHIARAMENTE INAPPROPRIATI (marca SI):\n"
            "- Vendo panieri aggiornati e materiale didattico a 30€, contattatemi in privato\n"
            "- Offro aiuto tesi a pagamento, scrivetemi su WhatsApp 123456789\n"
            "- Entra nella community! @panieriunipegasomercatorum - materiale a soli 15€\n"
            "- Ciao a tutti! Ho scoperto un sistema per guadagnare con il trading. Contattate @trader123 per info\n"
            "- Ciao a tutti😊 Se cercate panieri aggiornati e corretti con anche le risposte inedite di triennali e magistrali\n"
            "- **NUOVO**: Affidatevi all'unico canale ufficiale preposto alla vendita di panieri https://t.me/panieri\n"
            "- **NUOVO**: Qui sotto il link del canale dove iscriversi se volete panieri https://t.me/materiale\n\n"
            
            "ESEMPI DI MESSAGGI TRUFFA CRYPTO/TRADING (marca SI):\n"
            "- Ho trovato qualcuno di cui mi fido per fare trading di criptovalute. Contattala direttamente\n"
            "- Grazie a @expert_trader ho aumentato i miei guadagni del 200%, contattatelo\n\n"

            "ESEMPI DI MESSAGGI DI VENDITA DI PANIERI MASCHERATI (marca SI):\n"
            "- Ciao a tutti😊 Se cercate panieri aggiornati e corretti contattarmi\n"
            "- Ciao ragazzi, chi cerca panieri completi 2025 mi scriva\n\n"
            
            "ESEMPI DI MESSAGGI AMBIGUI MA APPROPRIATI (marca NO):\n"
            "- Ciao a tutti! Sto lavorando alla mia tesi e cerco partecipanti per un questionario. Ecco il link: https://forms.gle...\n"
            "- Salve, sono uno studente di economia e sto conducendo una ricerca, qualcuno può compilare questo form? https://forms.gle...\n"
            "- Qualcuno può passarmi i panieri aggiornati?\n"
            "- Chi ha i panieri di questo esame? Ne avrei bisogno urgentemente\n"
            "- Per favore mandate i numeri così vi aggiungo al gruppo WhatsApp\n\n"
            
            "CONTESTO UNIVERSITÀ TELEMATICHE:\n"
            "I panieri sono raccolte legittime di domande d'esame. È normale che gli studenti se li scambino gratuitamente. Solo la VENDITA di panieri o la promozione di canali esterni per materiale è inappropriata.\n\n"
            
            "IMPORTANTE: Se un messaggio non è CHIARAMENTE inappropriato secondo i criteri sopra, marcalo come APPROPRIATO. In caso di dubbio, è sempre meglio permettere un messaggio potenzialmente inappropriato piuttosto che bloccare un messaggio legittimo.\n\n"

            "ISTRUZIONI SPECIFICHE PER RICONOSCERE DOMANDE:\n"
            "Una domanda è un messaggio che richiede informazioni, chiarimenti, aiuto o conferma da altri utenti. Marca come DOMANDA: SI se:\n\n"
            
            "✅ CRITERI PER RICONOSCERE UNA DOMANDA:\n"
            "• Il messaggio contiene un punto interrogativo ?\n"
            "• Il messaggio inizia con parole interrogative: chi, cosa, come, dove, quando, perché, quale, quanto\n"
            "• Il messaggio chiede informazioni sulla piattaforma, accesso, corsi, esami, costi\n"
            "• Il messaggio richiede conferma con strutture come: 'qualcuno sa', 'c'è qualcuno', 'riuscite a', 'avete'\n"
            "• Il messaggio esprime una richiesta di aiuto o materiale\n"
            "• Il messaggio chiede opinioni o esperienze\n"
            "• Il messaggio usa il condizionale per chiedere informazioni: 'sapreste', 'potreste'\n"
            "• Il messaggio usa formule dirette come: 'mi serve sapere', 'cerco informazioni'\n\n"
            
            "ESEMPI DI DOMANDE DA RICONOSCERE CORRETTAMENTE (marca DOMANDA: SI):\n"
            "- oggi riuscite ad entrare in piattaforma pegaso?\n"
            "- Buongiorno quanto costa all inclusive se fatta al terzo anno?\n"
            "- C'è una rappresentante per lm77?\n"
            "- Qualcuno ha i panieri di storia medievale?\n"
            "- Sapete quando escono i risultati dell'esame di ieri?\n\n"
            
            "NON SONO DOMANDE (marca DOMANDA: NO):\n"
            "- Buongiorno a tutti\n"
            "- Ho superato l'esame finalmente!\n"
            "- Grazie mille per l'aiuto\n\n"
            
            "IMPORTANTE: Una domanda può essere formulata anche senza punto interrogativo, valuta il contesto e l'intento. Ogni richiesta di informazioni o aiuto è una domanda, anche se formulata come affermazione."
        )
        
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo", # Considera "gpt-3.5-turbo-0125" per risposte più strutturate o più recenti.
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message_text}
                ],
                temperature=0.0, # Bassa temperatura per risposte più deterministiche/fattuali
                max_tokens=50,   # Aumentato leggermente per sicurezza, ma il formato è breve
                timeout=15       # Timeout ragionevole
            )
            
            result_text = response.choices[0].message.content.strip() if response.choices[0].message.content else ""
            self.logger.debug(f"Risposta OpenAI: '{result_text}' per messaggio: '{message_text[:50]}...'")

            # Parsing robusto della risposta
            is_inappropriate = "INAPPROPRIATO: SI" in result_text
            is_question = "DOMANDA: SI" in result_text
            is_disallowed_language = "LINGUA: NON CONSENTITA" in result_text
            
            if "INAPPROPRIATO: SI" in result_text or "LINGUA: NON CONSENTITA" in result_text :
                 self.stats['ai_filter_violations'] +=1

            analysis_tuple = (is_inappropriate, is_question, is_disallowed_language)
            self.analysis_cache.set(message_text, analysis_tuple)
            return analysis_tuple

        except OpenAIError as e:
            self.logger.error(f"Errore API OpenAI: {e}", exc_info=True)
            # Fallback a controlli locali in caso di errore OpenAI
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            is_disallowed_lang_local = self.is_language_disallowed(message_text)
            return is_inappropriate_local, False, is_disallowed_lang_local
        except Exception as e: # Altre eccezioni generiche
            self.logger.error(f"Errore imprevisto durante l'analisi OpenAI: {e}", exc_info=True)
            is_inappropriate_local = self.contains_banned_word(message_text) or \
                                     self.contains_suspicious_contact_invitation(message_text)
            is_disallowed_lang_local = self.is_language_disallowed(message_text)
            return is_inappropriate_local, False, is_disallowed_lang_local
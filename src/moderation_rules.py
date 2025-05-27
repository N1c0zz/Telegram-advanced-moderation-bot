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
        # self.sheets_manager = sheets_manager # Non usato direttamente in questa classe
        
        self.banned_words: List[str] = self.config_manager.get('banned_words', [])
        self.allowed_languages: List[str] = self.config_manager.get('allowed_languages', ["italian"])
        
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
        Filtro meccanico ULTRA-MINIMALE - blocca solo spam ovvio.
        Tutto il resto passa a OpenAI.
        """
        normalized_text = self.normalize_text(text)
        if not normalized_text:
            return False

        # DEBUG: Log del testo normalizzato
        self.logger.debug(f"Filtro diretto - Testo originale: '{text}'")
        self.logger.debug(f"Filtro diretto - Testo normalizzato: '{normalized_text}'")

        # Test rapido per caratteri cirillici PRIMA della normalizzazione
        cyrillic_count = sum(1 for char in text if '\u0400' <= char <= '\u04FF' or '\u0500' <= char <= '\u052F')
        if cyrillic_count >= 3:  # Se ci sono almeno 3 caratteri cirillici
            self.logger.info(f"MATCH filtro diretto: {cyrillic_count} caratteri cirillici in '{text[:50]}...'")
            return True

        # SOLO spam ovvio e inequivocabile
        minimal_patterns = [
            # Vendita esplicita con prezzo E contatto privato (tutti insieme)
            r"(?:vendo|offro).*[0-9]+\s*(?:euro|€).*(?:scriv|contatt|privat|whatsapp|telegram)",
            
            # Account spam noti
            r"@panieriunipegasomercatorum",
            r"@unitelematica",
            
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

        for pattern in minimal_patterns:
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
        """Determina se la lingua del messaggio non è tra quelle consentite."""
        if not self.allowed_languages or "any" in self.allowed_languages:
            return False

        # PRIMO: Controllo rapido alfabeti non latini (PRIMA di langdetect)
        cyrillic_chars = sum(1 for char in text if '\u0400' <= char <= '\u04FF' or '\u0500' <= char <= '\u052F')
        arabic_chars = sum(1 for char in text if '\u0600' <= char <= '\u06FF')
        chinese_chars = sum(1 for char in text if '\u4e00' <= char <= '\u9fff')
        
        total_alpha_chars = len([c for c in text if c.isalpha()])
        
        if total_alpha_chars > 0:
            non_latin_ratio = (cyrillic_chars + arabic_chars + chinese_chars) / total_alpha_chars
            if non_latin_ratio > 0.3:  # Se più del 30% sono caratteri non latini
                self.logger.info(f"Lingua non consentita: {non_latin_ratio:.2%} caratteri non latini in '{text[:50]}...'")
                return True

        # NUOVO: Controllo parole inglesi comuni PRIMA di langdetect
        if total_alpha_chars >= 2:  # Almeno 2 lettere per questo controllo
            text_lower = text.lower().strip()
            
            # Lista parole inglesi comuni che NON dovrebbero passare
            common_english_words = [
                'hello', 'hi', 'hey', 'how', 'are', 'you', 'what', 'where', 'when', 
                'why', 'who', 'can', 'could', 'would', 'should', 'will', 'shall',
                'good', 'bad', 'nice', 'great', 'thank', 'thanks', 'please', 'sorry',
                'yes', 'no', 'okay', 'ok', 'welcome', 'bye', 'goodbye', 'see',
                'the', 'and', 'but', 'for', 'with', 'this', 'that', 'there',
                'have', 'has', 'had', 'was', 'were', 'been', 'being', 'make',
                'made', 'get', 'got', 'take', 'took', 'come', 'came', 'go', 'went'
            ]
            
            # Controlla se il messaggio è composto principalmente da parole inglesi
            words_in_message = text_lower.replace('.', '').replace('!', '').replace('?', '').split()
            if words_in_message:
                english_word_count = sum(1 for word in words_in_message if word in common_english_words)
                english_ratio = english_word_count / len(words_in_message)
                
                # Se più del 50% delle parole sono inglesi comuni, blocca
                if english_ratio > 0.5:
                    self.logger.info(f"Rilevato messaggio prevalentemente inglese: {english_ratio:.2%} parole inglesi comuni in '{text}'")
                    return True

        # SECONDO: Per testi lunghi, usa langdetect
        if total_alpha_chars < 8:  # Mantieni soglia originale per langdetect
            return False

        detected_lang_code = self.detect_language(text)
        if detected_lang_code:
            # Mappa le lingue consentite ai codici ISO
            lang_mapping = {
                'italian': 'it',
                'it': 'it'
            }
            
            allowed_codes = []
            for lang in self.allowed_languages:
                mapped = lang_mapping.get(lang.lower(), lang.lower())
                allowed_codes.append(mapped)
            
            if detected_lang_code not in allowed_codes:
                # CONTROLLO SPECIALE: Se rileva come non-italiano ma contiene parole chiaramente italiane
                italian_university_words = [
                    'università', 'esame', 'professore', 'crediti', 'corso', 'laurea', 
                    'triennale', 'magistrale', 'dottorato', 'facoltà', 'appunti', 
                    'panieri', 'lezioni', 'tesi', 'sessione', 'matricola', 'ateneo',
                    'dipartimento', 'cattedra', 'semestre', 'frequenza', 'iscrizione',
                    'buongiorno', 'buonasera', 'grazie', 'prego', 'ancora', 'non',
                    'oggi', 'domani', 'quando', 'dove', 'come', 'perché', 'cosa'
                ]
                
                text_lower = text.lower()
                italian_words_found = [word for word in italian_university_words if word in text_lower]
                
                if italian_words_found:
                    self.logger.info(f"Langdetect rileva '{detected_lang_code}' ma trovate parole italiane: {italian_words_found} in '{text}' - PERMESSO")
                    return False
                
                self.logger.info(f"Lingua non consentita rilevata: '{detected_lang_code}' (consentite: {allowed_codes}) per '{text[:50]}...'")
                return True

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
            "5. Verifica se il messaggio è una DOMANDA (con o senza punto interrogativo)\n"
            "6. Se hai dubbi, considera il messaggio APPROPRIATO\n\n"
            
            "DETTAGLIO DEI CRITERI:\n\n"
            
            "1️⃣ LINGUA (analizza per prima cosa):\n"
            f"Lingue consentite (codici ISO 639-1): {self.allowed_languages}\n" # Inietta le lingue consentite nel prompt
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
            "❌ Link a canali esterni con espliciti intenti commerciali:\n"
            "    • Entra nel nostro canale per acquistare materiale a prezzi scontati\n"
            "❌ Insulti pesanti diretti ad altri utenti:\n"
            "    • Offese personali gravi, linguaggio d'odio, minacce\n"
            "❌ Promozioni di investimenti o trading:\n"
            "    • Messaggi che promuovono guadagni facili attraverso trading o investimenti\n"
            "    • Messaggi che promuovono esperti di trading/investimenti da contattare\n"
            "    • Promozioni di servizi di consulenza per investimenti o trading\n"
            "    • Offerte di guadagno attraverso criptovalute o forex\n"
            "    • Messaggi che condividono link a gruppi o bot per investimenti\n\n"
            
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
            "\n⚠️ Esempi di link SEMPRE APPROPRIATI:\n"
            "    • https://chat.whatsapp.com/Ab12CdEfGhIj (semplice link senza testo aggiuntivo)\n"
            "    • Ecco il link: https://chat.whatsapp.com/Ab12CdEfGhIj\n\n"
            
            "ESEMPI DI MESSAGGI CHIARAMENTE INAPPROPRIATI (marca SI):\n"
            "- Vendo panieri aggiornati e materiale didattico a 30€, contattatemi in privato\n"
            "- Offro aiuto tesi a pagamento, scrivetemi su WhatsApp 123456789\n"
            "- Entra nella community! @panieriunipegasomercatorum - materiale a soli 15€\n"
            "- Ciao a tutti! Ho scoperto un sistema per guadagnare con il trading. Contattate @trader123 per info\n"
            "- Ciao a tutti😊 Se cercate panieri aggiornati e corretti con anche le risposte inedite di triennali e magistrali\n\n"
            
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
            "I panieri sono raccolte legittime di domande d'esame. È normale che gli studenti se li scambino gratuitamente. Solo la VENDITA di panieri è inappropriata.\n\n"
            
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
import streamlit as st
import sqlite3
from contextlib import contextmanager
from datetime import datetime
import pandas as pd
import re

DB_NAME = "portafoglio.db"

VALUTE = ["EUR", "USD", "GBP", "CHF", "JPY", "TRY", "BRL"]

OPERAZIONI = ["Acquisto", "Cedola/Dividendo", "Vendita"]

# Tipo di quotazione del titolo: le obbligazioni sono quotate in
# percentuale del valore nominale, azioni/ETF/fondi a prezzo unitario.
TIPI_PREZZO = {
    False: "Unitario (azioni, ETF, fondi)",
    True: "Percentuale del nominale (obbligazioni)"
}

# Aliquota sui redditi di capitale e sui capital gain: 26% ordinaria,
# 12,5% per titoli di Stato italiani ed emittenti "white list".
TASSAZIONE_DEFAULT = 26.0

COLONNE_TX = (
    "isin",
    "data",
    "operazione",
    "quantita",
    "prezzo_valuta",
    "cambio",
    "importo_euro",
    "commissioni_eur",
    "rateo_eur",
    "cedola_eur",
    "ritenuta_eur",
    "totale_valore_eur"
)

FORMATO_TRANSAZIONI = {
    "Quantità": "{:,.2f}",
    "Prezzo Valuta": "{:,.5f}",
    "Cambio": "{:,.4f}",
    "Importo (EUR)": "{:,.2f} €",
    "Commissioni (EUR)": "{:,.2f} €",
    "Rateo (EUR)": "{:,.2f} €",
    "Cedola (EUR)": "{:,.2f} €",
    "Ritenuta (EUR)": "{:,.2f} €",
    "Totale Valore (EUR)": "{:,.2f} €",
}


# ============================================================
# CONFIGURAZIONE
# ============================================================

st.set_page_config(
    page_title="Gestione Portafoglio Titoli",
    page_icon="💼",
    layout="wide"
)

COLONNE_TITOLI = {
    "isin": st.column_config.TextColumn("ISIN"),
    "nome_asset": st.column_config.TextColumn("Strumento"),
    "valuta": st.column_config.TextColumn("Valuta"),
    "prezzo_percentuale": st.column_config.CheckboxColumn("Prezzo in %"),
    "tassazione_pct": st.column_config.NumberColumn(
        "Tassazione %",
        format="%.2f %%"
    ),
    "ticker": st.column_config.TextColumn("Ticker")
}


# ============================================================
# DATABASE
# ============================================================

@contextmanager
def get_connection():
    """
    Connessione con commit automatico (rollback in caso di errore)
    e chiusura garantita all'uscita dal blocco with.
    """
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def inizializza_db():
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS titoli (
                isin TEXT PRIMARY KEY,
                nome_asset TEXT NOT NULL,
                valuta TEXT NOT NULL,
                prezzo_percentuale INTEGER NOT NULL DEFAULT 0,
                tassazione_pct REAL NOT NULL DEFAULT {TASSAZIONE_DEFAULT},
                ticker TEXT
            )
        """.format(TASSAZIONE_DEFAULT=TASSAZIONE_DEFAULT))

        # Migrazione dei database creati prima dell'introduzione
        # delle colonne aggiunte in seguito.
        colonne_titoli = [
            riga[1]
            for riga in cursor.execute("PRAGMA table_info(titoli)")
        ]

        colonne_aggiunte = {
            "prezzo_percentuale": "INTEGER NOT NULL DEFAULT 0",
            "tassazione_pct": f"REAL NOT NULL DEFAULT {TASSAZIONE_DEFAULT}",
            "ticker": "TEXT"
        }

        for colonna, definizione in colonne_aggiunte.items():
            if colonna not in colonne_titoli:
                cursor.execute(
                    f"ALTER TABLE titoli ADD COLUMN {colonna} {definizione}"
                )

        cursor.execute(
            SQL_CREA_TRANSAZIONI.format(nome="IF NOT EXISTS transazioni")
        )

    migra_fk_transazioni()


SQL_CREA_TRANSAZIONI = """
    CREATE TABLE {nome} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        isin TEXT NOT NULL,
        data TEXT NOT NULL,
        operazione TEXT NOT NULL,
        quantita REAL NOT NULL,
        prezzo_valuta REAL NOT NULL,
        cambio REAL NOT NULL,
        importo_euro REAL NOT NULL,
        commissioni_eur REAL NOT NULL,
        rateo_eur REAL NOT NULL,
        cedola_eur REAL NOT NULL,
        ritenuta_eur REAL NOT NULL,
        totale_valore_eur REAL NOT NULL,
        FOREIGN KEY (isin)
            REFERENCES titoli (isin)
            ON UPDATE CASCADE
            ON DELETE RESTRICT
    )
"""


def migra_fk_transazioni():
    """
    I database creati da versioni precedenti hanno la chiave esterna
    su transazioni senza ON UPDATE CASCADE: in quel caso non si può
    cambiare l'ISIN di un titolo con movimenti. SQLite non permette
    di modificare una chiave esterna, quindi la tabella viene
    ricostruita copiando i dati.
    """
    conn = sqlite3.connect(DB_NAME, isolation_level=None)

    try:
        # colonne di foreign_key_list: id, seq, table, from, to,
        # on_update, on_delete, match
        fk = conn.execute(
            "PRAGMA foreign_key_list(transazioni)"
        ).fetchall()

        if fk and fk[0][5] == "CASCADE":
            return

        # Va disattivato fuori dalla transazione
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")

        try:
            seq = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'transazioni'"
            ).fetchone()

            conn.execute(
                SQL_CREA_TRANSAZIONI.format(nome="transazioni_nuova")
            )
            conn.execute(
                f"""
                INSERT INTO transazioni_nuova (id, {", ".join(COLONNE_TX)})
                SELECT id, {", ".join(COLONNE_TX)} FROM transazioni
                """
            )
            conn.execute("DROP TABLE transazioni")
            conn.execute(
                "ALTER TABLE transazioni_nuova RENAME TO transazioni"
            )

            # Mantiene il contatore degli ID, per non riusare
            # gli ID di movimenti già eliminati.
            if seq is not None:
                conn.execute(
                    """
                    UPDATE sqlite_sequence
                    SET seq = MAX(seq, ?)
                    WHERE name = 'transazioni'
                    """,
                    (seq[0],)
                )

            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError(
                    "Movimenti collegati a titoli inesistenti."
                )

            conn.execute("COMMIT")

        except Exception:
            conn.execute("ROLLBACK")
            raise

    finally:
        conn.close()


inizializza_db()


# ============================================================
# FUNZIONI UTILI
# ============================================================

def valida_isin(isin):
    """
    Controllo base dell'ISIN:
    - 12 caratteri
    - lettere/numeri

    NB: questo NON verifica la cifra di controllo (checksum)
    prevista dallo standard ISO 6166. Controlla solo il formato.
    """
    isin = isin.strip().upper()
    return bool(re.fullmatch(r"[A-Z0-9]{12}", isin))


def valida_data(data_str):
    """
    Converte una data italiana gg/mm/aaaa
    nella rappresentazione SQL yyyy-mm-dd.
    """
    data_str = data_str.strip()

    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", data_str):
        raise ValueError(
            "La data deve essere nel formato gg/mm/aaaa."
        )

    try:
        data = datetime.strptime(data_str, "%d/%m/%Y")
    except ValueError:
        raise ValueError(
            "La data inserita non è valida."
        )

    return data.strftime("%Y-%m-%d")


def data_sql_to_it(data_sql):
    """
    Converte yyyy-mm-dd -> dd/mm/yyyy
    """
    return datetime.strptime(
        data_sql, "%Y-%m-%d"
    ).strftime("%d/%m/%Y")


def etichetta_titolo(row):
    return f"{row['isin']} - {row['nome_asset']} ({row['valuta']})"


def notifica(messaggio):
    """
    Memorizza un messaggio da mostrare dopo st.rerun():
    un st.success() seguito subito da st.rerun() non
    verrebbe mai visualizzato.
    """
    st.session_state["_notifica"] = messaggio


def ottieni_titoli():
    with get_connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT isin, nome_asset, valuta, prezzo_percentuale,
                   tassazione_pct, ticker
            FROM titoli
            ORDER BY nome_asset ASC
            """,
            conn
        )

    df["prezzo_percentuale"] = df["prezzo_percentuale"].astype(bool)
    df["ticker"] = df["ticker"].fillna("")
    return df


def ottieni_transazioni():
    with get_connection() as conn:
        query = """
            SELECT
                t.id AS ID,
                t.data AS Data,
                t.isin AS ISIN,
                am.nome_asset AS Asset,
                am.valuta AS Valuta,
                t.operazione AS Operazione,
                t.quantita AS Quantità,
                t.prezzo_valuta AS "Prezzo Valuta",
                t.cambio AS Cambio,
                t.importo_euro AS "Importo (EUR)",
                t.commissioni_eur AS "Commissioni (EUR)",
                t.rateo_eur AS "Rateo (EUR)",
                t.cedola_eur AS "Cedola (EUR)",
                t.ritenuta_eur AS "Ritenuta (EUR)",
                t.totale_valore_eur AS "Totale Valore (EUR)"
            FROM transazioni t
            JOIN titoli am
                ON t.isin = am.isin
            ORDER BY t.data DESC, t.id DESC
        """

        return pd.read_sql_query(query, conn)


def calcola_valori_finanziari(
    operazione,
    quantita,
    prezzo,
    cambio,
    commissioni,
    rateo,
    cedola,
    ritenuta,
    prezzo_percentuale=False
):
    """
    Valida i dati e calcola:
    - importo dell'operazione in EUR
    - totale economico della transazione

    Convenzioni adottate:

    ACQUISTO:
        importo + commissioni + rateo + ritenuta

    VENDITA:
        importo - commissioni + rateo + ritenuta

    CEDOLA/DIVIDENDO:
        cedola + ritenuta

    La ritenuta deve essere inserita con segno negativo.

    Con prezzo_percentuale=True (obbligazioni) la quantità è il
    valore nominale e il prezzo è in percentuale del nominale:
    importo = nominale * prezzo / 100 / cambio.
    """

    quantita = float(quantita)
    prezzo = float(prezzo)
    cambio = float(cambio)
    commissioni = float(commissioni)
    rateo = float(rateo)
    cedola = float(cedola)
    ritenuta = float(ritenuta)

    if operazione not in OPERAZIONI:
        raise ValueError(
            "Tipo di operazione non riconosciuto."
        )

    if commissioni < 0:
        raise ValueError(
            "Le commissioni non possono essere negative."
        )

    if cedola < 0:
        raise ValueError(
            "La cedola/dividendo non può essere negativa."
        )

    if ritenuta > 0:
        raise ValueError(
            "La ritenuta va inserita con segno negativo "
            "(es. -26.00)."
        )

    if operazione == "Cedola/Dividendo":

        if cedola <= 0:
            raise ValueError(
                "Per una cedola/dividendo l'importo lordo "
                "deve essere maggiore di zero."
            )

        importo_euro = 0.0
        totale_valore_eur = cedola + ritenuta

    else:

        if quantita <= 0:
            raise ValueError(
                "La quantità deve essere maggiore di zero."
            )

        if prezzo <= 0:
            raise ValueError(
                "Il prezzo deve essere maggiore di zero."
            )

        if cambio <= 0:
            raise ValueError(
                "Il tasso di cambio deve essere maggiore di zero."
            )

        importo_euro = (
            quantita * prezzo
        ) / cambio

        if prezzo_percentuale:
            importo_euro /= 100

        if operazione == "Acquisto":
            totale_valore_eur = (
                importo_euro
                + commissioni
                + rateo
                + ritenuta
            )

        else:
            totale_valore_eur = (
                importo_euro
                - commissioni
                + rateo
                + ritenuta
            )

    return (
        round(importo_euro, 2),
        round(totale_valore_eur, 2)
    )


# ============================================================
# TITOLI
# ============================================================

def tassazione_valida(tassazione_pct):
    return 0 <= float(tassazione_pct) <= 100


def normalizza_ticker(ticker):
    """Ticker Yahoo Finance facoltativo: stringa vuota -> None."""
    return (ticker or "").strip().upper() or None


def inserisci_titolo(
    isin,
    nome_asset,
    valuta,
    prezzo_percentuale,
    tassazione_pct,
    ticker=""
):

    isin = isin.strip().upper()
    nome_asset = nome_asset.strip()

    if not valida_isin(isin):
        return (
            False,
            "L'ISIN deve contenere esattamente 12 caratteri "
            "alfanumerici."
        )

    if not nome_asset:
        return False, "Il nome dello strumento è obbligatorio."

    if not tassazione_valida(tassazione_pct):
        return False, "La tassazione deve essere compresa tra 0 e 100%."

    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO titoli (
                    isin,
                    nome_asset,
                    valuta,
                    prezzo_percentuale,
                    tassazione_pct,
                    ticker
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    isin,
                    nome_asset,
                    valuta,
                    int(prezzo_percentuale),
                    float(tassazione_pct),
                    normalizza_ticker(ticker)
                )
            )

        return True, "Titolo registrato con successo!"

    except sqlite3.IntegrityError:
        return (
            False,
            "Errore: questo codice ISIN è già presente."
        )


# ============================================================
# GESTIONE ANAGRAFICA TITOLI
# ============================================================

def ricalcola_transazioni_titolo(conn, isin, prezzo_percentuale):
    """
    Ricalcola importo e totale di tutti i movimenti di un titolo
    (usato quando cambia il tipo di quotazione).
    Restituisce il numero di movimenti ricalcolati.
    """
    righe = conn.execute(
        """
        SELECT
            id,
            operazione,
            quantita,
            prezzo_valuta,
            cambio,
            commissioni_eur,
            rateo_eur,
            cedola_eur,
            ritenuta_eur
        FROM transazioni
        WHERE isin = ?
        """,
        (isin,)
    ).fetchall()

    for id_tx, *valori in righe:

        importo_euro, totale_valore_eur = calcola_valori_finanziari(
            *valori,
            prezzo_percentuale=prezzo_percentuale
        )

        conn.execute(
            """
            UPDATE transazioni
            SET importo_euro = ?, totale_valore_eur = ?
            WHERE id = ?
            """,
            (importo_euro, totale_valore_eur, id_tx)
        )

    return len(righe)


def aggiorna_titolo(
    isin_originale,
    nuovo_isin,
    nome_asset,
    valuta,
    prezzo_percentuale,
    tassazione_pct,
    ticker=""
):
    """
    Modifica un titolo esistente. Se cambia il tipo di quotazione,
    ricalcola i movimenti già registrati.
    """
    isin_originale = isin_originale.strip().upper()
    nuovo_isin = nuovo_isin.strip().upper()
    nome_asset = nome_asset.strip()

    # L'ISIN si valida solo se viene cambiato: così restano
    # modificabili anche titoli censiti prima della validazione.
    if nuovo_isin != isin_originale and not valida_isin(nuovo_isin):
        return False, "Il nuovo ISIN deve contenere esattamente 12 caratteri alfanumerici."

    if not nome_asset:
        return False, "Il nome dello strumento è obbligatorio."

    if not tassazione_valida(tassazione_pct):
        return False, "La tassazione deve essere compresa tra 0 e 100%."

    try:
        with get_connection() as conn:
            attuale = conn.execute(
                "SELECT prezzo_percentuale FROM titoli WHERE isin = ?",
                (isin_originale,)
            ).fetchone()

            if attuale is None:
                return False, "Titolo non trovato."

            if nuovo_isin != isin_originale and conn.execute(
                "SELECT 1 FROM titoli WHERE isin = ?",
                (nuovo_isin,)
            ).fetchone():
                return False, "Errore: il nuovo ISIN è già presente in anagrafica."

            conn.execute(
                """
                UPDATE titoli
                SET isin = ?, nome_asset = ?, valuta = ?,
                    prezzo_percentuale = ?, tassazione_pct = ?,
                    ticker = ?
                WHERE isin = ?
                """,
                (
                    nuovo_isin,
                    nome_asset,
                    valuta,
                    int(prezzo_percentuale),
                    float(tassazione_pct),
                    normalizza_ticker(ticker),
                    isin_originale
                )
            )

            messaggio = "Titolo modificato con successo."

            if bool(attuale[0]) != bool(prezzo_percentuale):
                ricalcolati = ricalcola_transazioni_titolo(
                    conn, nuovo_isin, prezzo_percentuale
                )

                if ricalcolati:
                    messaggio += f" Ricalcolati {ricalcolati} movimento/i."

        return True, messaggio

    except sqlite3.Error as e:
        return False, f"Errore database: {e}"

    except ValueError as e:
        return False, f"Impossibile ricalcolare i movimenti: {e}"


def elimina_titolo(isin):
    """Elimina un titolo solo se non esistono movimenti collegati."""
    isin = isin.strip().upper()

    try:
        with get_connection() as conn:
            movimenti = conn.execute(
                """
                SELECT COUNT(*)
                FROM transazioni
                WHERE isin = ?
                """,
                (isin,)
            ).fetchone()[0]

            if movimenti > 0:
                return (
                    False,
                    f"Il titolo non può essere eliminato perché ha "
                    f"{movimenti} movimento/i registrato/i. "
                    "Elimina prima i movimenti collegati, se necessario."
                )

            cursor = conn.execute(
                "DELETE FROM titoli WHERE isin = ?",
                (isin,)
            )

            if cursor.rowcount == 0:
                return False, "Titolo non trovato."

        return True, "Titolo eliminato con successo."

    except sqlite3.IntegrityError:
        return False, "Il titolo non può essere eliminato perché è utilizzato da movimenti esistenti."


def ottieni_transazioni_titolo(isin):
    """Restituisce lo storico dei movimenti di un singolo titolo."""
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                id AS ID,
                data AS Data,
                operazione AS Operazione,
                quantita AS Quantità,
                prezzo_valuta AS "Prezzo Valuta",
                cambio AS Cambio,
                importo_euro AS "Importo (EUR)",
                commissioni_eur AS "Commissioni (EUR)",
                rateo_eur AS "Rateo (EUR)",
                cedola_eur AS "Cedola (EUR)",
                ritenuta_eur AS "Ritenuta (EUR)",
                totale_valore_eur AS "Totale Valore (EUR)"
            FROM transazioni
            WHERE isin = ?
            ORDER BY data DESC, id DESC
            """,
            conn,
            params=(isin,)
        )


# ============================================================
# TRANSAZIONI
# ============================================================

def prepara_transazione(
    conn,
    isin,
    data_it,
    operazione,
    quantita,
    prezzo,
    cambio,
    commissioni,
    rateo,
    cedola,
    ritenuta
):
    """
    Valida i dati di un movimento e restituisce i valori
    delle colonne della tabella transazioni (ordine COLONNE_TX).
    """
    data_sql = valida_data(data_it)

    titolo = conn.execute(
        """
        SELECT prezzo_percentuale
        FROM titoli
        WHERE isin = ?
        """,
        (isin,)
    ).fetchone()

    if titolo is None:
        raise ValueError(
            "Il titolo selezionato non esiste nell'anagrafica."
        )

    importo_euro, totale_valore_eur = calcola_valori_finanziari(
        operazione,
        quantita,
        prezzo,
        cambio,
        commissioni,
        rateo,
        cedola,
        ritenuta,
        prezzo_percentuale=bool(titolo[0])
    )

    return (
        isin,
        data_sql,
        operazione,
        float(quantita),
        float(prezzo),
        float(cambio),
        importo_euro,
        float(commissioni),
        float(rateo),
        float(cedola),
        float(ritenuta),
        totale_valore_eur
    )


def inserisci_transazione(isin, **dati):

    with get_connection() as conn:

        valori = prepara_transazione(conn, isin, **dati)

        conn.execute(
            f"""
            INSERT INTO transazioni ({", ".join(COLONNE_TX)})
            VALUES ({", ".join("?" * len(COLONNE_TX))})
            """,
            valori
        )


def aggiorna_transazione(id_tx, isin, **dati):

    with get_connection() as conn:

        valori = prepara_transazione(conn, isin, **dati)

        cursor = conn.execute(
            f"""
            UPDATE transazioni
            SET {", ".join(f"{c} = ?" for c in COLONNE_TX)}
            WHERE id = ?
            """,
            (*valori, int(id_tx))
        )

        if cursor.rowcount == 0:
            raise ValueError("Transazione non trovata.")


def elimina_transazione(id_tx):

    with get_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM transazioni
            WHERE id = ?
            """,
            (int(id_tx),)
        )

        if cursor.rowcount == 0:
            raise ValueError("Transazione non trovata.")


# ============================================================
# RENDIMENTI
# ============================================================

def ottieni_movimenti_rendimenti():
    """Tutti i movimenti in ordine cronologico, con i dati del titolo."""
    with get_connection() as conn:
        return pd.read_sql_query(
            """
            SELECT
                t.id, t.isin, t.data, t.operazione, t.quantita,
                t.prezzo_valuta, t.cambio, t.importo_euro,
                t.commissioni_eur, t.rateo_eur, t.cedola_eur,
                t.ritenuta_eur, t.totale_valore_eur
            FROM transazioni t
            ORDER BY t.data ASC, t.id ASC
            """,
            conn
        )


def xirr(flussi):
    """
    Tasso interno di rendimento annuo di una serie di flussi
    [(data, importo)], con giorni/365 (come XIRR di Excel).
    Restituisce None se il tasso non è definito (flussi tutti
    dello stesso segno).
    """
    if not flussi:
        return None

    if not (
        any(v < 0 for _, v in flussi)
        and any(v > 0 for _, v in flussi)
    ):
        return None

    d0 = min(d for d, _ in flussi)

    def van(tasso):
        return sum(
            v / (1 + tasso) ** ((d - d0).days / 365.0)
            for d, v in flussi
        )

    basso, alto = -0.9999, 1.0

    while van(basso) * van(alto) > 0 and alto < 1e6:
        alto *= 2

    if van(basso) * van(alto) > 0:
        return None

    for _ in range(200):
        medio = (basso + alto) / 2

        if van(basso) * van(medio) <= 0:
            alto = medio
        else:
            basso = medio

    return (basso + alto) / 2


def segno_flusso(operazione):
    """Acquisto = uscita di cassa, vendita e cedola = entrata."""
    return -1 if operazione == "Acquisto" else 1


def analizza_titolo(
    movimenti,
    prezzo_percentuale,
    tassazione_pct,
    prezzo_attuale=None,
    cambio_attuale=None,
    oggi=None
):
    """
    Calcola risultati e indici di rendimento di un titolo a partire
    dai suoi movimenti (ordine cronologico) e, per la parte ancora
    in portafoglio, dal prezzo e cambio attuali.

    Metodo del costo medio ponderato. Il carico è tenuto in tre
    modi per separare gli effetti:
    - "eur": importo + commissioni in EUR (risultato effettivo)
    - "imp": solo importo in EUR (per l'effetto cambio)
    - "val": importo in valuta del titolo (variazione di prezzo)

    Le imposte sul capital gain sono una stima: aliquota del titolo
    sulle plusvalenze, senza compensazione con minusvalenze.
    """
    oggi = oggi or datetime.today().date()
    aliquota = float(tassazione_pct) / 100
    moltiplicatore = 0.01 if prezzo_percentuale else 1.0

    quantita = 0.0
    carico = {"eur": 0.0, "imp": 0.0, "val": 0.0}
    investito = {"eur": 0.0, "imp": 0.0, "val": 0.0}
    guadagno = {"eur": 0.0, "imp": 0.0, "val": 0.0}

    redditi_lordi = 0.0      # cedole + ratei incassati - ratei pagati
    ritenute = 0.0           # negative, come registrate
    imposte_cg = 0.0
    flussi_lordi = []        # prima di ritenute e imposte
    flussi_netti = []        # dopo ritenute e imposte stimate
    avvisi = []
    prima_data = None
    ultima_data = None

    for m in movimenti.itertuples():

        data = datetime.strptime(m.data, "%Y-%m-%d").date()
        prima_data = prima_data or data
        ultima_data = data

        ritenute += m.ritenuta_eur
        valore_valuta = m.quantita * m.prezzo_valuta * moltiplicatore
        imposta = 0.0

        if m.operazione == "Acquisto":

            costi = {
                "eur": m.importo_euro + m.commissioni_eur,
                "imp": m.importo_euro,
                "val": valore_valuta
            }

            for k in carico:
                carico[k] += costi[k]
                investito[k] += costi[k]

            quantita += m.quantita
            redditi_lordi -= m.rateo_eur

        elif m.operazione == "Vendita":

            if m.quantita > quantita + 1e-9:
                avvisi.append(
                    f"La vendita del {data:%d/%m/%Y} supera la "
                    "quantità in portafoglio."
                )

            frazione = min(m.quantita / quantita, 1.0) if quantita > 0 else 0.0

            ricavi = {
                "eur": m.importo_euro - m.commissioni_eur,
                "imp": m.importo_euro,
                "val": valore_valuta
            }

            for k in carico:
                costo_venduto = carico[k] * frazione
                if k == "eur":
                    imposta = max(ricavi[k] - costo_venduto, 0.0) * aliquota
                guadagno[k] += ricavi[k] - costo_venduto
                carico[k] -= costo_venduto

            imposte_cg += imposta
            quantita = max(quantita - m.quantita, 0.0)
            redditi_lordi += m.rateo_eur

        else:
            redditi_lordi += m.cedola_eur

        segno = segno_flusso(m.operazione)

        flussi_lordi.append(
            (data, segno * (m.totale_valore_eur - m.ritenuta_eur))
        )
        flussi_netti.append(
            (data, segno * m.totale_valore_eur - imposta)
        )

    aperta = quantita > 1e-9
    valutata = True
    valore_attuale = 0.0
    realizzato_eur = guadagno["eur"]
    latente_eur = 0.0

    if aperta:

        if prezzo_attuale and cambio_attuale:

            valore_valuta = quantita * prezzo_attuale * moltiplicatore
            valore = {
                "eur": valore_valuta / cambio_attuale,
                "imp": valore_valuta / cambio_attuale,
                "val": valore_valuta
            }

            for k in guadagno:
                guadagno[k] += valore[k] - carico[k]

            valore_attuale = valore["eur"]
            latente_eur = valore["eur"] - carico["eur"]
            imposta_latente = max(latente_eur, 0.0) * aliquota
            imposte_cg += imposta_latente

            flussi_lordi.append((oggi, valore_attuale))
            flussi_netti.append((oggi, valore_attuale - imposta_latente))

        else:
            valutata = False

    fine = oggi if aperta else ultima_data
    anni = (fine - prima_data).days / 365.0 if prima_data else 0.0

    def quota(numeratore, denominatore):
        return numeratore / denominatore if denominatore else None

    risultato = {
        "quantita_aperta": quantita,
        "aperta": aperta,
        "valutata": valutata,
        "anni": anni,
        "investito": investito["eur"],
        "carico_residuo": carico["eur"],
        "valore_attuale": valore_attuale if valutata else None,
        "pl_realizzato": realizzato_eur,
        "pl_latente": latente_eur if valutata else None,
        "redditi_lordi": redditi_lordi,
        "ritenute": ritenute,
        "imposte_cg": imposte_cg if valutata else None,
        "flussi_lordi": flussi_lordi,
        "flussi_netti": flussi_netti,
        "avvisi": avvisi
    }

    if valutata:
        lordo = guadagno["eur"] + redditi_lordi
        netto = lordo + ritenute - imposte_cg
        var_eur = quota(guadagno["imp"], investito["imp"])
        var_valuta = quota(guadagno["val"], investito["val"])

        risultato.update({
            "risultato_lordo": lordo,
            "risultato_netto": netto,
            "rend_lordo": quota(lordo, investito["eur"]),
            "rend_netto": quota(netto, investito["eur"]),
            "xirr_lordo": xirr(flussi_lordi),
            "xirr_netto": xirr(flussi_netti),
            "var_prezzo_valuta": var_valuta,
            "effetto_cambio": (
                var_eur - var_valuta
                if var_eur is not None and var_valuta is not None
                else None
            ),
            "proventi_annui": (
                quota(redditi_lordi + ritenute, investito["eur"]) / anni
                if anni > 0 and investito["eur"]
                else None
            )
        })

    return risultato


# ------------------------------------------------------------
# PREZZI ONLINE (Yahoo Finance)
# ------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def ticker_da_isin(isin):
    """Cerca su Yahoo Finance il simbolo corrispondente a un ISIN."""
    try:
        import yfinance as yf
        risultati = yf.Search(isin, max_results=1).quotes
        return risultati[0]["symbol"] if risultati else None
    except Exception:
        return None


@st.cache_data(ttl=3600, show_spinner=False)
def quotazione_online(ticker):
    """
    Ultimo prezzo di chiusura disponibile: dict con prezzo, valuta
    e data, oppure None se il simbolo non è disponibile.
    """
    try:
        import yfinance as yf
        titolo = yf.Ticker(ticker)
        storico = titolo.history(period="1mo")

        if storico.empty:
            return None

        prezzo = float(storico["Close"].iloc[-1])
        valuta = getattr(titolo.fast_info, "currency", None)

        # Le azioni di Londra sono quotate in pence
        if valuta in ("GBp", "GBX"):
            prezzo /= 100
            valuta = "GBP"

        return {
            "prezzo": prezzo,
            "valuta": valuta,
            "data": storico.index[-1].date()
        }

    except Exception:
        return None


def cambio_online(valuta):
    """Unità di valuta per 1 EUR, la stessa convenzione dei movimenti."""
    if valuta == "EUR":
        return 1.0

    quotazione = quotazione_online(f"EUR{valuta}=X")
    return quotazione["prezzo"] if quotazione else None


def valutazione_online(titolo):
    """
    Prezzo e cambio attuali di un titolo da Yahoo Finance, con
    l'indicazione della fonte da mostrare all'utente.
    """
    ticker = titolo["ticker"] or ticker_da_isin(titolo["isin"])
    cambio = cambio_online(titolo["valuta"])

    if not ticker:
        return None, cambio, "", None, "Non trovato online"

    quotazione = quotazione_online(ticker)

    if quotazione is None:
        return None, cambio, ticker, None, "Prezzo non disponibile"

    if quotazione["valuta"] and quotazione["valuta"] != titolo["valuta"]:
        return (
            None, cambio, ticker, None,
            f"Quotato in {quotazione['valuta']}, non {titolo['valuta']}"
        )

    return quotazione["prezzo"], cambio, ticker, quotazione["data"], "Online"


# ============================================================
# MODULO MOVIMENTO (condiviso da inserimento e modifica)
# ============================================================

# Campi azzerati dopo un inserimento riuscito: data, tipo
# operazione e cambio restano, per velocizzare inserimenti
# in serie. Azzerare quantità/cedola evita anche il doppio
# inserimento con un secondo clic.
CAMPI_DA_AZZERARE = ("qta", "prezzo", "comm", "rateo", "cedola", "ritenuta")


def azzera_campi_movimento(prefisso):
    for nome in CAMPI_DA_AZZERARE:
        st.session_state.pop(f"{prefisso}_{nome}", None)


def campi_movimento(prefisso, titolo, default=None, n_colonne=3):
    """
    Disegna i campi di un movimento sul titolo indicato, con
    l'anteprima del calcolo, e restituisce i valori inseriti
    (con i nomi dei parametri di inserisci_transazione).
    Il prefisso rende univoche le key dei widget.
    """
    d = {
        "data_it": datetime.today().strftime("%d/%m/%Y"),
        "operazione": "Acquisto",
        "quantita": 0.0,
        "prezzo": 0.0,
        "cambio": 1.0,
        "commissioni": 0.0,
        "rateo": 0.0,
        "cedola": 0.0,
        "ritenuta": 0.0
    }

    if default:
        d.update(default)

    valuta = titolo["valuta"]
    percentuale = bool(titolo["prezzo_percentuale"])

    # 9 campi distribuiti in ordine sulle colonne disponibili
    colonne = st.columns(n_colonne)
    posti = [colonne[i * n_colonne // 9] for i in range(9)]

    v = {}

    with posti[0]:
        v["data_it"] = st.text_input(
            "Data Operazione (gg/mm/aaaa)",
            value=d["data_it"],
            key=f"{prefisso}_data"
        )

    with posti[1]:
        v["operazione"] = st.selectbox(
            "Tipo Operazione",
            OPERAZIONI,
            index=OPERAZIONI.index(d["operazione"]),
            key=f"{prefisso}_op"
        )

    with posti[2]:
        v["quantita"] = st.number_input(
            "Valore nominale" if percentuale else "Quantità",
            min_value=0.0,
            step=100.0,
            value=float(d["quantita"]),
            key=f"{prefisso}_qta"
        )

    with posti[3]:
        v["prezzo"] = st.number_input(
            f"Prezzo (% del nominale, {valuta})"
            if percentuale
            else f"Prezzo in valuta ({valuta})",
            min_value=0.0,
            format="%.5f",
            value=float(d["prezzo"]),
            key=f"{prefisso}_prezzo"
        )

    with posti[4]:
        if valuta == "EUR":
            v["cambio"] = st.number_input(
                "Tasso di Cambio",
                value=1.0,
                disabled=True,
                key=f"{prefisso}_cambio_eur"
            )
        else:
            v["cambio"] = st.number_input(
                f"Tasso di Cambio ({valuta} per 1 EUR)",
                min_value=0.0001,
                format="%.4f",
                value=max(float(d["cambio"]), 0.0001),
                key=f"{prefisso}_cambio"
            )

    with posti[5]:
        v["commissioni"] = st.number_input(
            "Commissioni operative (EUR)",
            min_value=0.0,
            step=1.0,
            value=float(d["commissioni"]),
            key=f"{prefisso}_comm"
        )

    with posti[6]:
        v["rateo"] = st.number_input(
            "Rateo (EUR)",
            step=1.0,
            value=float(d["rateo"]),
            key=f"{prefisso}_rateo"
        )

    with posti[7]:
        v["cedola"] = st.number_input(
            "Cedola / Dividendo lordo (EUR)",
            min_value=0.0,
            step=1.0,
            value=float(d["cedola"]),
            key=f"{prefisso}_cedola"
        )

    with posti[8]:
        v["ritenuta"] = st.number_input(
            "Ritenuta / Tasse (EUR)",
            max_value=0.0,
            step=1.0,
            value=min(float(d["ritenuta"]), 0.0),
            key=f"{prefisso}_ritenuta",
            help="Inserisci la ritenuta con segno negativo. "
                 "Esempio: -26.00"
        )

    # ----------------------------------------------------
    # ANTEPRIMA CALCOLO
    # ----------------------------------------------------

    try:

        importo_preview, totale_preview = calcola_valori_finanziari(
            v["operazione"],
            v["quantita"],
            v["prezzo"],
            v["cambio"],
            v["commissioni"],
            v["rateo"],
            v["cedola"],
            v["ritenuta"],
            prezzo_percentuale=percentuale
        )

        c1, c2 = st.columns(2)

        with c1:
            st.metric(
                "Importo operazione",
                f"{importo_preview:,.2f} €"
            )

        with c2:
            st.metric(
                "Totale valore",
                f"{totale_preview:,.2f} €"
            )

    except ValueError as e:

        st.info(f"Anteprima non disponibile: {e}")

    return v


# ============================================================
# HEADER
# ============================================================

st.title("💼 Gestione Portafoglio Titoli")

st.caption(
    "Anagrafica strumenti, transazioni, cedole/dividendi "
    "e storico delle operazioni."
)

if "_notifica" in st.session_state:
    st.success(st.session_state.pop("_notifica"))


tab1, tab_rend, tab2, tab3, tab4 = st.tabs([
    "📊 Registro Transazioni",
    "📈 Rendimenti",
    "➕ Registra Movimento",
    "🎫 Anagrafica Titoli",
    "🆕 Nuovo Titolo"
])


# ============================================================
# TAB 1
# REGISTRO TRANSAZIONI
# ============================================================

with tab1:

    st.subheader("Storico dei Movimenti")

    df_report = ottieni_transazioni()

    if not df_report.empty:

        df_visualizzazione = df_report.copy()

        df_visualizzazione["Data"] = (
            pd.to_datetime(
                df_visualizzazione["Data"]
            ).dt.strftime("%d/%m/%Y")
        )

        st.dataframe(
            df_visualizzazione.style.format(FORMATO_TRANSAZIONI),
            width="stretch",
            height=400,
            hide_index=True
        )

        st.divider()

        # ====================================================
        # MODIFICA
        # ====================================================

        col_mod, col_del = st.columns(2)

        with col_mod:

            with st.expander(
                "✏️ Modifica una transazione"
            ):

                lista_id = df_report["ID"].tolist()

                id_selezionato = st.selectbox(
                    "Seleziona ID",
                    lista_id,
                    key="sel_mod"
                )

                riga = df_report[
                    df_report["ID"] == id_selezionato
                ].iloc[0]

                df_titoli_all = ottieni_titoli()

                mappa_titoli = {
                    etichetta_titolo(row): row
                    for _, row
                    in df_titoli_all.iterrows()
                }

                lista_opzioni = list(
                    mappa_titoli.keys()
                )

                opzione_default = etichetta_titolo({
                    "isin": riga["ISIN"],
                    "nome_asset": riga["Asset"],
                    "valuta": riga["Valuta"]
                })

                index_default = (
                    lista_opzioni.index(
                        opzione_default
                    )
                    if opzione_default in lista_opzioni
                    else 0
                )

                # Le key includono l'ID della transazione
                # selezionata: senza, Streamlit manterrebbe i
                # valori digitati per la transazione precedente.
                prefisso_mod = f"mod_{id_selezionato}"

                titolo_mod = st.selectbox(
                    "Strumento finanziario",
                    lista_opzioni,
                    index=index_default,
                    key=f"{prefisso_mod}_titolo"
                )

                titolo_info = mappa_titoli[titolo_mod]

                valori_mod = campi_movimento(
                    prefisso_mod,
                    titolo_info,
                    default={
                        "data_it": data_sql_to_it(riga["Data"]),
                        "operazione": riga["Operazione"],
                        "quantita": riga["Quantità"],
                        "prezzo": riga["Prezzo Valuta"],
                        "cambio": riga["Cambio"],
                        "commissioni": riga["Commissioni (EUR)"],
                        "rateo": riga["Rateo (EUR)"],
                        "cedola": riga["Cedola (EUR)"],
                        "ritenuta": riga["Ritenuta (EUR)"]
                    },
                    n_colonne=2
                )

                if st.button(
                    "💾 Salva Modifiche",
                    type="primary",
                    key=f"{prefisso_mod}_btn_aggiorna"
                ):

                    try:

                        aggiorna_transazione(
                            id_selezionato,
                            titolo_info["isin"],
                            **valori_mod
                        )

                        notifica(
                            "Transazione modificata correttamente."
                        )

                        st.rerun()

                    except ValueError as e:

                        st.error(str(e))

                    except sqlite3.Error as e:

                        st.error(f"Errore database: {e}")

        # ====================================================
        # ELIMINAZIONE
        # ====================================================

        with col_del:

            with st.expander(
                "🗑️ Elimina una transazione"
            ):

                id_da_eliminare = st.selectbox(
                    "Seleziona ID da eliminare",
                    df_report["ID"].tolist(),
                    key="sel_del"
                )

                riga_del = df_report[
                    df_report["ID"] == id_da_eliminare
                ].iloc[0]

                st.warning(
                    f"Transazione ID {id_da_eliminare}: "
                    f"{riga_del['Operazione']} - "
                    f"{riga_del['Asset']}"
                )

                # Key con suffisso per non ereditare la spunta
                # di conferma da un ID selezionato in precedenza.
                conferma = st.checkbox(
                    "Confermo di voler eliminare definitivamente "
                    "questa transazione.",
                    key=f"conferma_delete_{id_da_eliminare}"
                )

                if st.button(
                    "🗑️ Elimina definitivamente",
                    type="primary",
                    disabled=not conferma,
                    key=f"btn_elimina_tx_{id_da_eliminare}"
                ):

                    try:

                        elimina_transazione(
                            id_da_eliminare
                        )

                        notifica(
                            "Transazione eliminata."
                        )

                        st.rerun()

                    except (ValueError, sqlite3.Error) as e:

                        st.error(str(e))

    else:

        st.info(
            "Nessun movimento presente nel database."
        )


# ============================================================
# TAB RENDIMENTI
# ============================================================

def percentuale(valore):
    return None if valore is None else valore * 100


COLONNA_EURO = st.column_config.NumberColumn(format="%.2f €")
COLONNA_PCT = st.column_config.NumberColumn(format="%.2f %%")

with tab_rend:

    st.subheader("📈 Rendimento degli investimenti")

    df_mov_rend = ottieni_movimenti_rendimenti()

    if df_mov_rend.empty:

        st.info("Nessun movimento presente nel database.")

    else:

        df_titoli_rend = ottieni_titoli()
        df_titoli_rend = df_titoli_rend[
            df_titoli_rend["isin"].isin(df_mov_rend["isin"])
        ]

        movimenti_per_titolo = {
            isin: gruppo
            for isin, gruppo in df_mov_rend.groupby("isin", sort=False)
        }

        def analizza(titolo, prezzo=None, cambio=None):
            return analizza_titolo(
                movimenti_per_titolo[titolo["isin"]],
                titolo["prezzo_percentuale"],
                titolo["tassazione_pct"],
                prezzo,
                cambio
            )

        # Prima passata senza prezzi: serve a sapere quali
        # posizioni sono ancora aperte e per quale quantità.
        analisi_base = {
            titolo["isin"]: analizza(titolo)
            for _, titolo in df_titoli_rend.iterrows()
        }

        # ----------------------------------------------------
        # VALUTAZIONE POSIZIONI APERTE
        # ----------------------------------------------------

        titoli_aperti = df_titoli_rend[
            [analisi_base[i]["aperta"] for i in df_titoli_rend["isin"]]
        ]

        valutazioni = {}

        if not titoli_aperti.empty:

            col_titolo, col_bottone = st.columns([4, 1])

            with col_titolo:
                st.markdown("#### Valutazione delle posizioni aperte")

            with col_bottone:
                if st.button("🔄 Aggiorna prezzi", key="rend_aggiorna"):
                    ticker_da_isin.clear()
                    quotazione_online.clear()
                    st.session_state.pop("rend_editor", None)

            righe_valutazione = []

            with st.spinner("Recupero prezzi da Yahoo Finance..."):
                for _, titolo in titoli_aperti.iterrows():
                    prezzo, cambio, ticker, data_prezzo, fonte = (
                        valutazione_online(titolo)
                    )
                    righe_valutazione.append({
                        "ISIN": titolo["isin"],
                        "Strumento": titolo["nome_asset"],
                        "Ticker": ticker,
                        "Quantità": analisi_base[titolo["isin"]]["quantita_aperta"],
                        "Valuta": titolo["valuta"],
                        "Prezzo attuale": prezzo,
                        "Cambio": cambio,
                        "Data prezzo": data_prezzo,
                        "Fonte": fonte
                    })

            st.caption(
                "Prezzo e cambio si possono correggere o inserire "
                "direttamente in tabella (restano validi fino alla "
                "chiusura della pagina). Per le obbligazioni il prezzo "
                "è in % del nominale; il cambio è in unità di valuta "
                "per 1 EUR."
            )

            df_valutazione = st.data_editor(
                pd.DataFrame(righe_valutazione),
                key="rend_editor",
                hide_index=True,
                width="stretch",
                disabled=[
                    "ISIN", "Strumento", "Ticker", "Quantità",
                    "Valuta", "Data prezzo", "Fonte"
                ],
                column_config={
                    "Quantità": st.column_config.NumberColumn(format="%.2f"),
                    "Prezzo attuale": st.column_config.NumberColumn(
                        min_value=0.0, format="%.4f"
                    ),
                    "Cambio": st.column_config.NumberColumn(
                        min_value=0.0001, format="%.4f"
                    ),
                    "Data prezzo": st.column_config.DateColumn(
                        format="DD/MM/YYYY"
                    )
                }
            )

            for _, riga in df_valutazione.iterrows():
                prezzo = riga["Prezzo attuale"]
                cambio = 1.0 if riga["Valuta"] == "EUR" else riga["Cambio"]
                valutazioni[riga["ISIN"]] = (
                    None if pd.isna(prezzo) else float(prezzo),
                    None if pd.isna(cambio) else float(cambio)
                )

        # ----------------------------------------------------
        # CALCOLO
        # ----------------------------------------------------

        analisi = {}

        for _, titolo in df_titoli_rend.iterrows():
            prezzo, cambio = valutazioni.get(titolo["isin"], (None, None))
            analisi[titolo["isin"]] = analizza(titolo, prezzo, cambio)

        for _, titolo in df_titoli_rend.iterrows():
            for avviso in analisi[titolo["isin"]]["avvisi"]:
                st.warning(f"{titolo['nome_asset']}: {avviso}")

        non_valutati = [
            titolo["nome_asset"]
            for _, titolo in df_titoli_rend.iterrows()
            if not analisi[titolo["isin"]]["valutata"]
        ]

        # ----------------------------------------------------
        # RIEPILOGO PORTAFOGLIO
        # ----------------------------------------------------

        st.markdown("#### Portafoglio")

        if non_valutati:

            st.warning(
                "Manca il prezzo attuale di: "
                + ", ".join(non_valutati)
                + ". Inseriscilo nella tabella sopra per calcolare "
                "i rendimenti di questi titoli e del portafoglio."
            )

        else:

            investito_tot = sum(a["investito"] for a in analisi.values())
            valore_tot = sum(a["valore_attuale"] for a in analisi.values())
            netto_tot = sum(a["risultato_netto"] for a in analisi.values())
            xirr_tot = xirr([
                flusso
                for a in analisi.values()
                for flusso in a["flussi_netti"]
            ])

            k1, k2, k3, k4 = st.columns(4)

            k1.metric("Capitale investito", f"{investito_tot:,.2f} €")
            k2.metric("Valore attuale", f"{valore_tot:,.2f} €")
            k3.metric(
                "Risultato netto",
                f"{netto_tot:,.2f} €",
                f"{netto_tot / investito_tot:.2%}" if investito_tot else None
            )
            k4.metric(
                "Rendimento annuo netto (XIRR)",
                f"{xirr_tot:.2%}" if xirr_tot is not None else "—"
            )

        # ----------------------------------------------------
        # DETTAGLIO PER TITOLO
        # ----------------------------------------------------

        righe_risultati = []
        righe_indici = []

        for _, titolo in df_titoli_rend.iterrows():

            a = analisi[titolo["isin"]]

            righe_risultati.append({
                "Strumento": titolo["nome_asset"],
                "Stato": "Aperta" if a["aperta"] else "Chiusa",
                "Investito": a["investito"],
                "Valore attuale": a["valore_attuale"],
                "P/L realizzato": a["pl_realizzato"],
                "P/L latente": a["pl_latente"],
                "Cedole e ratei lordi": a["redditi_lordi"],
                "Ritenute": a["ritenute"],
                "Imposte CG stimate": (
                    None if a["imposte_cg"] is None else -a["imposte_cg"]
                ),
                "Risultato netto": a.get("risultato_netto")
            })

            righe_indici.append({
                "Strumento": titolo["nome_asset"],
                "Durata (anni)": a["anni"],
                "Rend. lordo %": percentuale(a.get("rend_lordo")),
                "Rend. netto %": percentuale(a.get("rend_netto")),
                "XIRR lordo %": percentuale(a.get("xirr_lordo")),
                "XIRR netto %": percentuale(a.get("xirr_netto")),
                "Proventi netti % annuo": percentuale(a.get("proventi_annui")),
                "Var. prezzo in valuta %": percentuale(a.get("var_prezzo_valuta")),
                "Effetto cambio (punti %)": percentuale(a.get("effetto_cambio"))
            })

        st.markdown("#### Risultati in euro")

        st.dataframe(
            pd.DataFrame(righe_risultati),
            hide_index=True,
            width="stretch",
            column_config={
                colonna: COLONNA_EURO
                for colonna in righe_risultati[0]
                if colonna not in ("Strumento", "Stato")
            }
        )

        st.markdown("#### Indici di rendimento")

        st.dataframe(
            pd.DataFrame(righe_indici),
            hide_index=True,
            width="stretch",
            column_config={
                "Durata (anni)": st.column_config.NumberColumn(format="%.2f"),
                **{
                    colonna: COLONNA_PCT
                    for colonna in righe_indici[0]
                    if colonna not in ("Strumento", "Durata (anni)")
                }
            }
        )

        with st.expander("ℹ️ Come leggere gli indici"):
            st.markdown(
                """
- **Investito**: somma degli acquisti (controvalore + commissioni).
- **P/L realizzato / latente**: plusvalenza (o minusvalenza) sulla
  parte venduta / ancora in portafoglio, con il metodo del
  **costo medio ponderato**, commissioni incluse.
- **Cedole e ratei lordi**: cedole/dividendi incassati + ratei
  incassati alla vendita − ratei pagati all'acquisto.
- **Imposte CG stimate**: aliquota del titolo ("Tassazione %")
  applicata alle plusvalenze realizzate e latenti. È una stima:
  non considera la compensazione con minusvalenze.
- **Risultato netto**: P/L + cedole e ratei − ritenute − imposte.
- **Rend. lordo / netto %**: risultato / capitale investito, non
  annualizzato.
- **XIRR**: rendimento annuo composto che tiene conto di **quando**
  sono avvenuti i flussi (money-weighted). È l'indice più adatto a
  confrontare investimenti con durate diverse; su periodi brevi
  (pochi mesi) può risultare molto amplificato.
- **Proventi netti % annuo**: cedole e ratei netti in rapporto al
  capitale investito, per anno di detenzione.
- **Var. prezzo in valuta %**: variazione del capitale misurata
  nella valuta del titolo, senza commissioni.
- **Effetto cambio**: differenza (in punti percentuali) tra la
  variazione del capitale in EUR e quella in valuta: indica quanto
  il cambio ha aggiunto o tolto al rendimento.
- Le posizioni aperte sono valutate a oggi con il prezzo della
  tabella; le chiuse fino alla data dell'ultimo movimento.
                """
            )


# ============================================================
# TAB 2
# REGISTRA MOVIMENTO
# ============================================================

with tab2:

    st.subheader(
        "Inserisci una nuova operazione finanziaria"
    )

    df_titoli = ottieni_titoli()

    if df_titoli.empty:

        st.warning(
            "Per registrare un movimento devi prima "
            "censire almeno un titolo nel tab "
            "'Nuovo Titolo'."
        )

    else:

        mappa_titoli = {
            etichetta_titolo(row): row
            for _, row in df_titoli.iterrows()
        }

        opzione_scelta = st.selectbox(
            "Strumento finanziario",
            list(mappa_titoli.keys()),
            key="tx_titolo"
        )

        titolo_info = mappa_titoli[
            opzione_scelta
        ]

        valori_tx = campi_movimento("tx", titolo_info)

        # ----------------------------------------------------
        # SALVATAGGIO
        # ----------------------------------------------------

        if st.button(
            "💾 Salva Movimento nel Database",
            type="primary",
            key="btn_salva_tx"
        ):

            try:

                inserisci_transazione(
                    titolo_info["isin"],
                    **valori_tx
                )

                notifica(
                    "Operazione registrata nel database!"
                )

                azzera_campi_movimento("tx")

                st.rerun()

            except ValueError as e:

                st.error(str(e))

            except sqlite3.Error as e:

                st.error(
                    f"Errore database: {e}"
                )


# ============================================================
# TAB 3
# ANAGRAFICA TITOLI
# ============================================================

with tab3:

    st.subheader("📚 Gestione Anagrafica Titoli")
    st.caption(
        "Da questa sezione puoi modificare ed eliminare gli "
        "strumenti già censiti e registrare i movimenti "
        "direttamente sul titolo selezionato. Per censire un "
        "titolo nuovo usa il tab 'Nuovo Titolo'."
    )

    # --------------------------------------------------------
    # ELENCO TITOLI
    # --------------------------------------------------------

    df_anagrafica = ottieni_titoli()

    if df_anagrafica.empty:

        st.info(
            "Nessun titolo censito. Vai al tab '🆕 Nuovo Titolo' "
            "per aggiungerne uno."
        )

    else:

        col_search, col_count = st.columns([4, 1])

        with col_search:
            ricerca = st.text_input(
                "🔎 Cerca per ISIN o descrizione",
                placeholder="Es. XS2408944242 oppure EBRD",
                key="ricerca_anagrafica"
            ).strip().lower()

        df_elenco = df_anagrafica.copy()

        if ricerca:
            df_elenco = df_elenco[
                df_elenco["isin"].str.lower().str.contains(ricerca, na=False, regex=False)
                | df_elenco["nome_asset"].str.lower().str.contains(ricerca, na=False, regex=False)
            ]

        with col_count:
            st.metric("Titoli", len(df_elenco))

        st.dataframe(
            df_elenco,
            width="stretch",
            hide_index=True,
            column_config=COLONNE_TITOLI
        )

        if df_elenco.empty:
            st.warning("Nessun titolo corrisponde alla ricerca.")
        else:

            st.divider()

            # ------------------------------------------------
            # SELEZIONE TITOLO
            # ------------------------------------------------

            mappa_titoli_anagrafica = {
                etichetta_titolo(row): row
                for _, row in df_elenco.iterrows()
            }

            titolo_selezionato = st.selectbox(
                "Titolo da gestire",
                list(mappa_titoli_anagrafica.keys()),
                key="anagrafica_titolo_selezionato"
            )

            titolo = mappa_titoli_anagrafica[titolo_selezionato]
            isin_selezionato = titolo["isin"]

            # Suffisso comune per tutte le key, così i campi si
            # aggiornano correttamente quando si cambia titolo.
            suf_titolo = f"_{isin_selezionato}"

            # ------------------------------------------------
            # DATI E OPERAZIONI SUL TITOLO
            # ------------------------------------------------

            col_info, col_edit, col_delete = st.columns([2, 2, 2])

            with col_info:
                st.markdown("### ℹ️ Dati titolo")
                st.write(f"**ISIN:** {titolo['isin']}")
                st.write(f"**Descrizione:** {titolo['nome_asset']}")
                st.write(f"**Valuta:** {titolo['valuta']}")
                st.write(f"**Prezzo:** {TIPI_PREZZO[bool(titolo['prezzo_percentuale'])]}")
                st.write(f"**Tassazione:** {titolo['tassazione_pct']:.2f} %")
                st.write(f"**Ticker:** {titolo['ticker'] or '— (ricerca da ISIN)'}")

                if not valida_isin(titolo["isin"]):
                    st.warning(
                        "L'ISIN di questo titolo non è nel formato "
                        "standard (12 caratteri alfanumerici)."
                    )

                df_movimenti_titolo = ottieni_transazioni_titolo(isin_selezionato)

                st.metric(
                    "Movimenti registrati",
                    len(df_movimenti_titolo)
                )

            with col_edit:
                st.markdown("### ✏️ Modifica titolo")

                with st.form(f"form_modifica_titolo{suf_titolo}"):

                    modifica_isin = st.text_input(
                        "Codice ISIN",
                        value=titolo["isin"],
                        key=f"modifica_isin{suf_titolo}"
                    ).strip().upper()

                    modifica_nome = st.text_input(
                        "Nome Strumento / Descrizione",
                        value=titolo["nome_asset"],
                        key=f"modifica_nome{suf_titolo}"
                    ).strip()

                    indice_valuta = (
                        VALUTE.index(titolo["valuta"])
                        if titolo["valuta"] in VALUTE
                        else 0
                    )

                    modifica_valuta = st.selectbox(
                        "Valuta",
                        VALUTE,
                        index=indice_valuta,
                        key=f"modifica_valuta{suf_titolo}"
                    )

                    modifica_percentuale = st.selectbox(
                        "Tipo di prezzo",
                        list(TIPI_PREZZO),
                        index=int(bool(titolo["prezzo_percentuale"])),
                        format_func=TIPI_PREZZO.get,
                        key=f"modifica_percentuale{suf_titolo}"
                    )

                    modifica_tassazione = st.number_input(
                        "Tassazione %",
                        min_value=0.0,
                        max_value=100.0,
                        step=0.5,
                        format="%.2f",
                        value=float(titolo["tassazione_pct"]),
                        key=f"modifica_tassazione{suf_titolo}",
                        help="Aliquota su capital gain e cedole/dividendi "
                             "(es. 26 ordinaria, 12,5 titoli di Stato)."
                    )

                    modifica_ticker = st.text_input(
                        "Ticker (facoltativo)",
                        value=titolo["ticker"] or "",
                        key=f"modifica_ticker{suf_titolo}",
                        help="Simbolo Yahoo Finance per il prezzo online, es. ENI.MI. "
                             "Se vuoto viene cercato dall'ISIN."
                    )

                    if len(df_movimenti_titolo) > 0:
                        st.caption(
                            "Il titolo ha movimenti registrati: "
                            "cambiando il tipo di prezzo vengono "
                            "ricalcolati automaticamente, cambiando "
                            "la valuta no."
                        )

                    salva_titolo = st.form_submit_button(
                        "💾 Salva modifiche",
                        type="primary"
                    )

                if salva_titolo:

                    successo, messaggio = aggiorna_titolo(
                        isin_selezionato,
                        modifica_isin,
                        modifica_nome,
                        modifica_valuta,
                        modifica_percentuale,
                        modifica_tassazione,
                        modifica_ticker
                    )

                    if successo:
                        notifica(messaggio)
                        st.rerun()
                    else:
                        st.error(messaggio)

            with col_delete:
                st.markdown("### 🗑️ Elimina titolo")

                st.warning(
                    "L'eliminazione è consentita solo se il titolo "
                    "non ha movimenti collegati."
                )

                # Key con suffisso per non ereditare la spunta di
                # conferma da un titolo selezionato in precedenza.
                conferma_eliminazione = st.checkbox(
                    "Confermo l'eliminazione definitiva",
                    key=f"conferma_elimina_titolo{suf_titolo}"
                )

                if st.button(
                    "🗑️ Elimina titolo",
                    type="primary",
                    disabled=not conferma_eliminazione,
                    key=f"btn_elimina_titolo{suf_titolo}"
                ):

                    successo, messaggio = elimina_titolo(
                        isin_selezionato
                    )

                    if successo:
                        notifica(messaggio)
                        st.rerun()
                    else:
                        st.error(messaggio)

            st.divider()

            # ------------------------------------------------
            # REGISTRA MOVIMENTO SUL TITOLO SELEZIONATO
            # ------------------------------------------------

            st.subheader(
                f"➕ Registra movimento — {titolo['nome_asset']}"
            )

            with st.expander(
                "Apri il modulo per registrare un movimento",
                expanded=True
            ):

                prefisso_anag = f"anag{suf_titolo}"

                valori_anag = campi_movimento(prefisso_anag, titolo)

                if st.button(
                    "💾 Registra movimento",
                    type="primary",
                    key=f"btn_anag_salva_movimento{suf_titolo}"
                ):

                    try:

                        inserisci_transazione(
                            isin_selezionato,
                            **valori_anag
                        )

                        notifica(
                            "Movimento registrato correttamente."
                        )

                        azzera_campi_movimento(prefisso_anag)

                        st.rerun()

                    except ValueError as e:
                        st.error(str(e))

                    except sqlite3.Error as e:
                        st.error(f"Errore database: {e}")

            # ------------------------------------------------
            # STORICO DEL TITOLO
            # ------------------------------------------------

            st.divider()
            st.subheader("📜 Storico movimenti del titolo")

            if df_movimenti_titolo.empty:

                st.info(
                    "Nessun movimento registrato per questo titolo."
                )

            else:

                df_storico = df_movimenti_titolo.copy()

                df_storico["Data"] = (
                    pd.to_datetime(
                        df_storico["Data"]
                    ).dt.strftime("%d/%m/%Y")
                )

                st.dataframe(
                    df_storico.style.format(FORMATO_TRANSAZIONI),
                    width="stretch",
                    height=300,
                    hide_index=True
                )


# ============================================================
# TAB 4
# NUOVO TITOLO
# ============================================================

with tab4:

    st.subheader("🆕 Censisci un nuovo titolo")
    st.caption(
        "Aggiungi un nuovo strumento finanziario all'anagrafica "
        "prima di poter registrare movimenti su di esso."
    )

    with st.form("form_nuovo_titolo", clear_on_submit=True):

        col1, col2 = st.columns(2)

        with col1:
            nuovo_isin_input = st.text_input(
                "Codice ISIN",
                placeholder="Es. XS2408944242",
                max_chars=12,
                key="nuovo_titolo_isin"
            )

            nuovo_nome_input = st.text_input(
                "Nome Strumento / Descrizione",
                placeholder="Es. EBRD 2028",
                key="nuovo_titolo_nome"
            )

        with col2:
            nuova_valuta_input = st.selectbox(
                "Valuta",
                VALUTE,
                key="nuovo_titolo_valuta"
            )

            nuovo_percentuale_input = st.selectbox(
                "Tipo di prezzo",
                list(TIPI_PREZZO),
                format_func=TIPI_PREZZO.get,
                key="nuovo_titolo_percentuale",
                help="Le obbligazioni sono quotate in percentuale "
                     "del valore nominale (es. 98,50)."
            )

            nuova_tassazione_input = st.number_input(
                "Tassazione %",
                min_value=0.0,
                max_value=100.0,
                step=0.5,
                format="%.2f",
                value=TASSAZIONE_DEFAULT,
                key="nuovo_titolo_tassazione",
                help="Aliquota su capital gain e cedole/dividendi "
                     "(es. 26 ordinaria, 12,5 titoli di Stato)."
            )

            nuovo_ticker_input = st.text_input(
                "Ticker (facoltativo)",
                placeholder="Es. ENI.MI",
                key="nuovo_titolo_ticker",
                help="Simbolo Yahoo Finance per il prezzo online, es. ENI.MI. "
                     "Se vuoto viene cercato dall'ISIN."
            )

        submit_nuovo_titolo = st.form_submit_button(
            "💾 Registra Titolo",
            type="primary"
        )

    if submit_nuovo_titolo:

        successo, messaggio = inserisci_titolo(
            nuovo_isin_input,
            nuovo_nome_input,
            nuova_valuta_input,
            nuovo_percentuale_input,
            nuova_tassazione_input,
            nuovo_ticker_input
        )

        if successo:
            notifica(messaggio)
            st.rerun()
        else:
            st.error(messaggio)

    st.divider()

    df_titoli_esistenti = ottieni_titoli()

    if not df_titoli_esistenti.empty:
        st.caption(f"Titoli già censiti: {len(df_titoli_esistenti)}")
        st.dataframe(
            df_titoli_esistenti,
            width="stretch",
            hide_index=True,
            column_config=COLONNE_TITOLI
        )

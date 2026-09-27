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
    "prezzo_percentuale": st.column_config.CheckboxColumn("Prezzo in %")
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
                prezzo_percentuale INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Migrazione dei database creati prima dell'introduzione
        # del tipo di quotazione.
        colonne_titoli = [
            riga[1]
            for riga in cursor.execute("PRAGMA table_info(titoli)")
        ]

        if "prezzo_percentuale" not in colonne_titoli:
            cursor.execute("""
                ALTER TABLE titoli
                ADD COLUMN prezzo_percentuale INTEGER NOT NULL DEFAULT 0
            """)

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
            SELECT isin, nome_asset, valuta, prezzo_percentuale
            FROM titoli
            ORDER BY nome_asset ASC
            """,
            conn
        )

    df["prezzo_percentuale"] = df["prezzo_percentuale"].astype(bool)
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

def inserisci_titolo(isin, nome_asset, valuta, prezzo_percentuale):

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

    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO titoli (
                    isin,
                    nome_asset,
                    valuta,
                    prezzo_percentuale
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    isin,
                    nome_asset,
                    valuta,
                    int(prezzo_percentuale)
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
    prezzo_percentuale
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
                    prezzo_percentuale = ?
                WHERE isin = ?
                """,
                (
                    nuovo_isin,
                    nome_asset,
                    valuta,
                    int(prezzo_percentuale),
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


tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Registro Transazioni",
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
                        modifica_percentuale
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

        submit_nuovo_titolo = st.form_submit_button(
            "💾 Registra Titolo",
            type="primary"
        )

    if submit_nuovo_titolo:

        successo, messaggio = inserisci_titolo(
            nuovo_isin_input,
            nuovo_nome_input,
            nuova_valuta_input,
            nuovo_percentuale_input
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

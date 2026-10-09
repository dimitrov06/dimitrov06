from decimal import Decimal as D

from conftest import NOW
from signal_copier.models import IncomingMessage
from signal_copier.storage import Storage


def test_message_dedupe_keeps_each_edit_version():
    st = Storage(":memory:")
    m = IncomingMessage(chat_id=1, message_id=5, text="a", date=NOW)
    assert st.save_message(m) is True
    assert st.save_message(m) is False
    assert st.save_message(m.model_copy(update={"text": "b", "edited": True})) is True
    assert st.last_message_id(1) == 5


def test_order_insert_is_idempotent():
    st = Storage(":memory:")
    assert st.create_order("c1", "k", 0, "EUR_USD", D(10), "MARKET", None, D(1), None)
    assert not st.create_order("c1", "k", 0, "EUR_USD", D(10), "MARKET", None, D(1), None)
    st.update_order("c1", status="PENDING", response={"x": D("1.5")})
    assert st.open_positions() == [{"signal_key": "k", "instrument": "EUR_USD"}]


def test_state_roundtrip(tmp_path):
    st = Storage(tmp_path / "db" / "x.sqlite")
    st.set_state("halt_manual", "yes")
    assert st.get_state("halt_manual") == "yes"
    st.set_state("halt_manual", None)
    assert st.get_state("halt_manual") is None

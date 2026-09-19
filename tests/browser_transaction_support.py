"""Synthetic checkout content shared by real-browser transaction tests."""


def checkout_html() -> bytes:
    background = "\n".join(
        f"<section><h2>Account information {index}</h2><p>Background content</p></section>"
        for index in range(180)
    )
    return (
        """<!doctype html><html><head><title>Fixture account</title></head><body>
<h1>Account credits</h1><p>Current credit balance: $6.42</p>
<button type="button" onclick="document.querySelector('dialog').showModal()">Open checkout</button>
"""
        + background
        + """
<dialog aria-label="Purchase Credits">
<h2>Purchase Credits</h2>
<p>Payment method: Saved account</p>
<form action="/checkout-complete" method="post">
<label for="amount">Credit amount (USD)</label>
<input id="amount" name="credits" type="number" min="5" max="25000" value="10" step="1">
<p>Service fee: $<output id="fee">0.80</output></p>
<p>Total charge: $<output id="total">10.80</output></p>
<input id="charged" name="charged" type="hidden" value="10.80">
<p>One-time payment. No subscription.</p>
<button type="submit">Purchase credits</button>
<button type="button" onclick="this.closest('dialog').close()">Cancel</button>
</form></dialog>
<script>
document.querySelector('#amount').addEventListener('input', event => {
 const amount = Number(event.target.value);
 const fee = amount * 0.08;
 document.querySelector('#fee').textContent = fee.toFixed(2);
 document.querySelector('#total').textContent = (amount + fee).toFixed(2);
 document.querySelector('#charged').value = (amount + fee).toFixed(2);
});
</script></body></html>"""
    ).encode()

"""Inventory table override that exposes every physical-item barcode."""

from flask import redirect, render_template, request, url_for


def configure(app, server):
    def inventory_index():
        view = request.args.get('view', 'cards')
        q = request.args.get('q', '').strip()
        status = request.args.get('status', '').strip()

        with server.db() as conn:
            if q:
                exact = conn.execute(
                    '''SELECT p.id
                       FROM item_barcodes ib
                       JOIN products p ON p.id=ib.product_id
                       WHERE ib.barcode=? AND p.archived_at IS NULL
                       LIMIT 1''',
                    (q,),
                ).fetchone()
                if exact:
                    return redirect(url_for('product_detail', product_id=exact['id']))

            sql = '''SELECT p.*, ph.filename AS main_photo,
                     (SELECT MIN(ib.barcode)
                        FROM item_barcodes ib
                       WHERE ib.product_id=p.id) AS first_barcode,
                     (SELECT GROUP_CONCAT(ib.barcode, '||')
                        FROM item_barcodes ib
                       WHERE ib.product_id=p.id
                       ORDER BY ib.id) AS all_barcodes
                     FROM products p
                     LEFT JOIN photos ph
                       ON ph.id=(SELECT id FROM photos WHERE product_id=p.id ORDER BY sort_order,id LIMIT 1)
                     WHERE p.archived_at IS NULL'''
            params = []

            if q:
                like = f'%{q}%'
                sql += ''' AND (p.item LIKE ? OR p.main_colour LIKE ? OR p.secondary_colour LIKE ?
                           OR p.pattern LIKE ? OR p.notes LIKE ?
                           OR EXISTS(SELECT 1 FROM item_barcodes ib
                                      WHERE ib.product_id=p.id AND ib.barcode LIKE ?))'''
                params.extend([like] * 6)

            if status in ('Available', 'Sold'):
                sql += ' AND p.status=?'
                params.append(status)
            elif status == 'No photos':
                sql += ' AND NOT EXISTS(SELECT 1 FROM photos np WHERE np.product_id=p.id)'

            sql += ' ORDER BY p.id DESC'
            products = conn.execute(sql, params).fetchall()
            counts = {
                row['status']: row['c']
                for row in conn.execute(
                    "SELECT status,COUNT(*) c FROM products WHERE archived_at IS NULL GROUP BY status"
                ).fetchall()
            }

        return render_template(
            'index.html',
            products=products,
            view=view,
            q=q,
            status=status,
            available=counts.get('Available', 0),
            sold=counts.get('Sold', 0),
        )

    app.view_functions['index'] = inventory_index

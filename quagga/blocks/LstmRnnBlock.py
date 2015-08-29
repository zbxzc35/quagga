from itertools import izip
from quagga.matrix import Matrix
from quagga.context import Context
from quagga.matrix import MatrixList
from quagga.connector import Connector


class LstmRnnBlock(object):
    def __init__(self,
                 W_z_init, W_i_init, W_f_init, W_o_init,
                 R_z_init, R_i_init, R_f_init, R_o_init,
                 x, mask=None, reverse=False, learning=True, device_id=None):
        """
        TODO
        """
        for W_init, R_init in izip([W_z_init, W_i_init, W_f_init, W_o_init],
                                   [R_z_init, R_i_init, R_f_init, R_o_init]):
            if W_init.ncols != R_init.ncols:
                raise ValueError('W and R must have the same number of columns!')
            if R_init.nrows != R_init.ncols:
                raise ValueError('R must be a square matrix!')

        input_dim = W_init.nrows
        hidden_dim = R_init.nrows
        self.context = Context(device_id)
        device_id = self.context.device_id
        self.max_input_sequence_len = len(x)
        self.reverse = reverse

        W = [Matrix.from_npa(init(), device_id=device_id) for init in [W_z_init, W_i_init, W_f_init, W_o_init]]
        self.W = Matrix.empty(input_dim, 4 * hidden_dim, W[0].dtype, device_id)
        self.W.assign_hstack(self.context, W)
        if learning:
            self.dL_dW = Matrix.empty_like(self.W, device_id)

        R = [Matrix.from_npa(init(), device_id=device_id) for init in [R_z_init, R_i_init, R_f_init, R_o_init]]
        self.R = Matrix.empty(hidden_dim, 4 * hidden_dim, R[0].dtype, device_id)
        self.R.assign_hstack(self.context, R)
        if learning:
            self.dL_dR = Matrix.empty_like(self.R, device_id)

        self.x = x
        self.mask = mask.register_usage(self.context) if mask else None
        self.h = []
        self.lstm_cells = []
        batch_size = x[0].nrows
        for k in xrange(self.max_input_sequence_len):
            if k == 0:
                prev_c = Matrix.empty(batch_size, hidden_dim, device_id=device_id)
                prev_c.sync_fill(0.0)
                prev_h = prev_c
            else:
                prev_c = self.lstm_cells[-1].c
                prev_h = self.lstm_cells[-1].h
            k = self.max_input_sequence_len - 1 - k if reverse else k
            mask = self.mask[:, k] if self.mask else None
            if learning:
                cell = _LstmBlock(self.W, self.R, x[k], mask, prev_c, prev_h, device_id, self.dL_dW, self.dL_dR)
            else:
                cell = _LstmBlock(self.W, self.R, x[k], mask, prev_c, prev_h, device_id)
            self.lstm_cells.append(cell)
            self.h.append(cell.h)
        self.h = MatrixList(self.h[::-1] if reverse else self.h)

    def fprop(self):
        n = len(self.x)
        if n > self.max_input_sequence_len:
            raise ValueError('Sequence has length: {} that is too long. '
                             'The maximum is: {}'.
                             format(n, self.max_input_sequence_len))
        self.h.set_length(n)
        if self.reverse:
            if n != self.max_input_sequence_len:
                k = self.max_input_sequence_len - n
                self.lstm_cells[k].prev_c.fill(self.lstm_cells[k].context, 0.0)
                self.lstm_cells[k].prev_h.fill(self.lstm_cells[k].context, 0.0)
            if self.mask:
                # waiting for mask
                k = self.max_input_sequence_len - n
                self.lstm_cells[k].context.wait(self.context)
            for k in xrange(self.max_input_sequence_len - n, self.max_input_sequence_len):
                self.lstm_cells[k].fprop()
        else:
            if self.mask:
                # waiting for mask
                self.lstm_cells[0].context.wait(self.context)
            for k in xrange(n):
                self.lstm_cells[k].fprop()

    def bprop(self):
        self.dL_dW.fill(self.context, 0.0)
        self.dL_dR.fill(self.context, 0.0)
        n = len(self.x)
        if self.reverse:
            # waiting for filling dL_dW and dL_dR
            self.lstm_cells[-1].context.wait(self.context)
            for k in reversed(xrange(self.max_input_sequence_len - n, self.max_input_sequence_len)):
                self.lstm_cells[k].bprop()
        else:
            # waiting for filling dL_dW and dL_dR
            self.lstm_cells[n - 1].context.wait(self.context)
            if n != self.max_input_sequence_len:
                h, c = self.lstm_cells[n-1].h, self.lstm_cells[n-1].c
                h.deregistere_b_obtaining_context(self.lstm_cells[n].context)
                c.deregistere_b_obtaining_context(self.lstm_cells[n].context)
            for k in reversed(xrange(n)):
                self.lstm_cells[k].bprop()
            if n != self.max_input_sequence_len:
                h.remove_from_deregistered_b_obtaining_contexts(self.lstm_cells[n].context)
                c.remove_from_deregistered_b_obtaining_contexts(self.lstm_cells[n].context)

    @property
    def params(self):
        return [self.W, self.R]

    @property
    def grads(self):
        n = len(self.x)
        return [(self.lstm_cells[n].context, self.dL_dW), (self.lstm_cells[n].context, self.dL_dR)]


class _LstmBlock(object):
    def __init__(self, W, R, x, mask, prev_c, prev_h, device_id, dL_dW=None, dL_dR=None):
        """
        TODO

        :param W: matrix that contains horizontally stacked Wz, Wi, Wf, Wo
        :param R: matrix that contains horizontally stacked Rz, Ri, Rf, Ro
        :param prev_c: previous lstm cell state
        :param prev_h: previous lstm hidden state

        TODO
        """

        dim = R.nrows
        batch_size = prev_c.nrows
        # we must have different context for each cell in order to achieve
        # parallel execution os several gpu for multilayer LSTM
        self.context = Context(device_id)
        self.W = W
        learning = False
        if dL_dW:
            learning = True
            self.dL_dW = dL_dW
            self.dL_dR = dL_dR
        self.learning = learning

        self.R = R
        self.zifo = Matrix.empty(batch_size, 4 * dim, device_id=device_id)
        self.z = self.zifo[:, 0*dim:1*dim]
        self.i = self.zifo[:, 1*dim:2*dim]
        self.f = self.zifo[:, 2*dim:3*dim]
        self.o = self.zifo[:, 3*dim:4*dim]
        self.c = Matrix.empty_like(prev_c, device_id)
        self.c = Connector(self.c, self.context, self.context if learning else None)
        self.tanh_c = Matrix.empty_like(prev_c, device_id)
        self.h = Matrix.empty_like(prev_c, device_id)
        self.h = Connector(self.h, self.context, self.context if learning else None)

        if learning and x.bpropagable:
            self.x, self.dL_dx = x.register_usage(self.context, self.context)
        else:
            self.x = x.register_usage(self.context)
        self.mask = mask

        try:
            if learning:
                self.prev_c, self.dL_dprev_c = prev_c.register_usage(self.context, self.context)
                self.prev_h, self.dL_dprev_h = prev_h.register_usage(self.context, self.context)
            else:
                self.prev_c = prev_c.register_usage(self.context)
                self.prev_h = prev_h.register_usage(self.context)
            self.is_first = False
        except AttributeError:
            self.is_first = True
            self.prev_c = prev_c
            self.prev_h = prev_h

        if learning:
            self._dzifo_dpre_zifo = Matrix.empty_like(self.zifo, device_id)
            self.dz_dpre_z = self._dzifo_dpre_zifo[:, 0*dim:1*dim]
            self.di_dpre_i = self._dzifo_dpre_zifo[:, 1*dim:2*dim]
            self.df_dpre_f = self._dzifo_dpre_zifo[:, 2*dim:3*dim]
            self.do_dpre_o = self._dzifo_dpre_zifo[:, 3*dim:4*dim]
            self.dL_dpre_zifo = self._dzifo_dpre_zifo
            self.dL_dpre_z = self.dz_dpre_z
            self.dL_dpre_i = self.di_dpre_i
            self.dL_dpre_f = self.df_dpre_f
            self.dL_dpre_o = self.do_dpre_o
            self._dtanh_c_dc = Matrix.empty_like(self.c, device_id)

    @property
    def dzifo_dpre_zifo(self):
        if self.learning:
            return self._dzifo_dpre_zifo

    @property
    def dtanh_c_dc(self):
        if self.learning:
            return self._dtanh_c_dc

    def fprop(self):
        # zifo = tanh_sigm(x[t] * W + h[t-1] * R)
        self.zifo.assign_dot(self.context, self.x, self.W)
        self.zifo.add_dot(self.context, self.prev_h, self.R)
        self.zifo.tanh_sigm(self.context, self.zifo, self.dzifo_dpre_zifo, axis=1)

        # c[t] = i[t] .* z[t] + f[t] .* c[t-1]
        # h[t] = o[t] .* tanh(c[t])
        self.c.assign_sum_hprod(self.context, self.i, self.z, self.f, self.prev_c)
        self.c.tanh(self.context, self.tanh_c, self.dtanh_c_dc)
        self.h.assign_hprod(self.context, self.o, self.tanh_c)
        if self.mask:
            self.c.hprod(self.context, self.mask)
            self.h.hprod(self.context, self.mask)
        self.c.fprop()
        self.h.fprop()

    def bprop(self):
        # dL/dc[t] = dL[t+1]/dc[t] + dL/dh[t] .* o[t] .* dtanh(c[t])/dc[t]
        dL_dc = self.c.backward_matrix
        dL_dh = self.h.backward_matrix
        if self.mask:
            dL_dc.hprod(self.context, self.mask)
            dL_dh.hprod(self.context, self.mask)
        dL_dc.add_hprod(self.context, dL_dh, self.o, self.dtanh_c_dc)

        # dL/dpre_o[t] = dL/dh[t] .* tanh(c[t]) .* do[t]/dpre_o[t]
        # dL/dpre_f[t] = dL/dc[t] .* c[t-1] .* df[t]/dpre_f[t]
        # dL/dpre_i[t] = dL/dc[t] .* z[t] .* di[t]/dpre_i[t]
        # dL/dpre_z[t] = dL/dc[t] .* i[t] .* dz[t]/dpre_z[t]
        self.dL_dpre_o.assign_hprod(self.context, dL_dh, self.tanh_c, self.do_dpre_o)
        self.dL_dpre_f.assign_hprod(self.context, dL_dc, self.prev_c, self.df_dpre_f)
        self.dL_dpre_i.assign_hprod(self.context, dL_dc, self.z, self.di_dpre_i)
        self.dL_dpre_z.assign_hprod(self.context, dL_dc, self.i, self.dz_dpre_z)

        # dL_dW += x[t].T * dL/dpre_zifo[t]
        # dL_dR += h[t-1].T * dL/dpre_zifo[t]
        self.dL_dW.add_dot(self.context, self.x, self.dL_dpre_zifo, 'T')
        if not self.is_first:
            self.dL_dR.add_dot(self.context, self.prev_h, self.dL_dpre_zifo, 'T')

        if hasattr(self, 'dL_dx'):
            # dL/dx[t] = dL/dpre_zifo[t] * W.T
            self.dL_dx.assign_dot(self.context, self.dL_dpre_zifo, self.W, 'N', 'T')

        if hasattr(self, 'dL_dprev_h'):
            # dL/dc[t-1] = f[t] .* dL/dc[t]
            self.dL_dprev_c.assign_hprod(self.context, self.f, dL_dc)
            # dL/dh[t-1] = dL/dpre_zifo[t] * R.T
            self.dL_dprev_h.assign_dot(self.context, self.dL_dpre_zifo, self.R, 'N', 'T')
# pylint: disable=too-many-instance-attributes, too-many-arguments, protected-access
# pylint: disable=too-many-public-methods
"""A `BucketingModule` implement the `BaseModule` API, and allows multiple
symbols to be used depending on the `bucket_key` provided by each different
mini-batch of data.
"""

import logging
import warnings

from .. import context as ctx

from ..initializer import Uniform

from .base_module import BaseModule, _check_input_names
from .module import Module

class BucketingModule(BaseModule):
    """This module helps to deal efficiently with varying-length inputs.

    Parameters
    ----------
    sym_gen : function
        A function when called with a bucket key, returns a triple
        ``(symbol, data_names, label_names)``.
    default_bucket_key : str (or any python object)
        The key for the default bucket.
    logger : Logger
    context : Context or list of Context
        Defaults to ``mx.cpu()``
    work_load_list : list of number
        Defaults to ``None``, indicating uniform workload.
    fixed_param_names: list of str
        Defaults to ``None``, indicating no network parameters are fixed.
    state_names : list of str
        States are similar to data and label, but not provided by data iterator.
        Instead they are initialized to 0 and can be set by set_states()
    """
    def __init__(self, sym_gen, default_bucket_key=None, logger=logging,
                 context=ctx.cpu(), work_load_list=None,
                 fixed_param_names=None, state_names=None):
        super(BucketingModule, self).__init__(logger=logger)

        assert default_bucket_key is not None
        self._default_bucket_key = default_bucket_key
        self._sym_gen = sym_gen

        symbol, data_names, label_names = sym_gen(default_bucket_key)
        data_names = list(data_names) if data_names is not None else []
        label_names = list(label_names) if label_names is not None else []
        state_names = list(state_names) if state_names is not None else []
        fixed_param_names = list(fixed_param_names) if fixed_param_names is not None else []

        _check_input_names(symbol, data_names, "data", True)
        _check_input_names(symbol, label_names, "label", False)
        _check_input_names(symbol, state_names, "state", True)
        _check_input_names(symbol, fixed_param_names, "fixed_param", True)

        self._fixed_param_names = fixed_param_names
        self._state_names = state_names
        self._context = context
        self._work_load_list = work_load_list

        self._buckets = {}
        self._curr_module = None
        self._curr_bucket_key = None
        self._params_dirty = False

    def _reset_bind(self):
        """Internal utility function to reset binding."""
        self.binded = False
        self._buckets = {}
        self._curr_module = None
        self._curr_bucket_key = None

    @property
    def data_names(self):
        """A list of names for data required by this module."""
        if self.binded:
            return self._curr_module.data_names
        else:
            _, data_names, _ = self._sym_gen(self._default_bucket_key)
            return data_names

    @property
    def output_names(self):
        """A list of names for the outputs of this module."""
        if self.binded:
            return self._curr_module.output_names
        else:
            symbol, _, _ = self._sym_gen(self._default_bucket_key)
            return symbol.list_outputs()

    @property
    def data_shapes(self):
        """Get data shapes.
        Returns
        -------
        A list of `(name, shape)` pairs.
        """
        assert self.binded
        return self._curr_module.data_shapes

    @property
    def label_shapes(self):
        """Get label shapes.
        Returns
        -------
        A list of `(name, shape)` pairs. The return value could be ``None`` if
        the module does not need labels, or if the module is not bound for
        training (in this case, label information is not available).
        """
        assert self.binded
        return self._curr_module.label_shapes

    @property
    def output_shapes(self):
        """Get output shapes.
        Returns
        -------
        A list of `(name, shape)` pairs.
        """
        assert self.binded
        return self._curr_module.output_shapes

    def get_params(self):
        """Get current parameters.
        Returns
        -------
        `(arg_params, aux_params)`, each a dictionary mapping names to parameters
        (`NDArray`).
        """
        assert self.binded and self.params_initialized
        self._curr_module._params_dirty = self._params_dirty
        params = self._curr_module.get_params()
        self._params_dirty = False
        return params

    def set_params(self, arg_params, aux_params, allow_missing=False, force_init=True):
        """Assign parameter and aux state values.

        Parameters
        ----------
        arg_params : dict
            Dictionary of name to value (`NDArray`) mapping.
        aux_params : dict
            Dictionary of name to value (`NDArray`) mapping.
        allow_missing : bool
            If true, params could contain missing values, and the initializer will be
            called to fill those missing params.
        force_init : bool
            If true, will force re-initialize even if already initialized.

        Examples
        --------
        An example of setting module parameters::
            >>> sym, arg_params, aux_params = \
            >>>     mx.model.load_checkpoint(model_prefix, n_epoch_load)
            >>> mod.set_params(arg_params=arg_params, aux_params=aux_params)
        """
        if not allow_missing:
            self.init_params(initializer=None, arg_params=arg_params, aux_params=aux_params,
                             allow_missing=allow_missing, force_init=force_init)
            return

        if self.params_initialized and not force_init:
            warnings.warn("Parameters already initialized and force_init=False. "
                          "set_params call ignored.", stacklevel=2)
            return

        self._curr_module.set_params(arg_params, aux_params, allow_missing=allow_missing,
                                     force_init=force_init)

        # because we didn't update self._arg_params, they are dirty now.
        self._params_dirty = True
        self.params_initialized = True

    def init_params(self, initializer=Uniform(0.01), arg_params=None, aux_params=None,
                    allow_missing=False, force_init=False):
        """Initialize parameters.

        Parameters
        ----------
        initializer : Initializer
        arg_params : dict
            Defaults to ``None``. Existing parameters. This has higher priority
            than `initializer`.
        aux_params : dict
            Defaults to ``None``. Existing auxiliary states. This has higher priority
            than `initializer`.
        allow_missing : bool
            Allow missing values in `arg_params` and `aux_params` (if not ``None``).
            In this case, missing values will be filled with `initializer`.
        force_init : bool
            Defaults to ``False``.
        """
        if self.params_initialized and not force_init:
            return
        assert self.binded, 'call bind before initializing the parameters'
        self._curr_module.init_params(initializer=initializer, arg_params=arg_params,
                                      aux_params=aux_params, allow_missing=allow_missing,
                                      force_init=force_init)
        self._params_dirty = False
        self.params_initialized = True

    def get_states(self, merge_multi_context=True):
        """Get states from all devices

        Parameters
        ----------
        merge_multi_context : bool
            Default is `True`. In the case when data-parallelism is used, the states
            will be collected from multiple devices. A `True` value indicate that we
            should merge the collected results so that they look like from a single
            executor.

        Returns
        -------
        If `merge_multi_context` is ``True``, it is like ``[out1, out2]``. Otherwise, it
        is like ``[[out1_dev1, out1_dev2], [out2_dev1, out2_dev2]]``. All the output
        elements are `NDArray`.
        """
        assert self.binded and self.params_initialized
        return self._curr_module.get_states(merge_multi_context=merge_multi_context)

    def set_states(self, states=None, value=None):
        """Set value for states. Only one of states & value can be specified.

        Parameters
        ----------
        states : list of list of NDArrays
            Source states arrays formatted like ``[[state1_dev1, state1_dev2],
            [state2_dev1, state2_dev2]]``.
        value : number
            A single scalar value for all state arrays.
        """
        assert self.binded and self.params_initialized
        self._curr_module.set_states(states, value)

    def bind(self, data_shapes, label_shapes=None, for_training=True,
             inputs_need_grad=False, force_rebind=False, shared_module=None,
             grad_req='write'):
        """Binding for a `BucketingModule` means setting up the buckets and binding the
        executor for the default bucket key. Executors corresponding to other keys are
        bound afterwards with `switch_bucket`.

        Parameters
        ----------
        data_shapes : list of (str, tuple)
            This should correspond to the symbol for the default bucket.
        label_shapes : list of (str, tuple)
            This should correspond to the symbol for the default bucket.
        for_training : bool
            Default is ``True``.
        inputs_need_grad : bool
            Default is ``False``.
        force_rebind : bool
            Default is ``False``.
        shared_module : BucketingModule
            Default is ``None``. This value is currently not used.
        grad_req : str, list of str, dict of str to str
            Requirement for gradient accumulation. Can be 'write', 'add', or 'null'
            (default to 'write').
            Can be specified globally (str) or for each argument (list, dict).
        bucket_key : str (or any python object)
            bucket key for binding. by default use the default_bucket_key
        """
        # in case we already initialized params, keep it
        if self.params_initialized:
            arg_params, aux_params = self.get_params()

        # force rebinding is typically used when one want to switch from
        # training to prediction phase.
        if force_rebind:
            self._reset_bind()

        if self.binded:
            self.logger.warning('Already bound, ignoring bind()')
            return

        assert shared_module is None, 'shared_module for BucketingModule is not supported'

        self.for_training = for_training
        self.inputs_need_grad = inputs_need_grad
        self.binded = True

        symbol, data_names, label_names = self._sym_gen(self._default_bucket_key)
        module = Module(symbol, data_names, label_names, logger=self.logger,
                        context=self._context, work_load_list=self._work_load_list,
                        fixed_param_names=self._fixed_param_names,
                        state_names=self._state_names)
        module.bind(data_shapes, label_shapes, for_training, inputs_need_grad,
                    force_rebind=False, shared_module=None, grad_req=grad_req)
        self._curr_module = module
        self._curr_bucket_key = self._default_bucket_key
        self._buckets[self._default_bucket_key] = module

        # copy back saved params, if already initialized
        if self.params_initialized:
            self.set_params(arg_params, aux_params)

    def switch_bucket(self, bucket_key, data_shapes, label_shapes=None):
        """Switch to a different bucket. This will change ``self.curr_module``.

        Parameters
        ----------
        bucket_key : str (or any python object)
            The key of the target bucket.
        data_shapes : list of (str, tuple)
            Typically ``data_batch.provide_data``.
        label_shapes : list of (str, tuple)
            Typically ``data_batch.provide_label``.
        """
        assert self.binded, 'call bind before switching bucket'
        if not bucket_key in self._buckets:
            symbol, data_names, label_names = self._sym_gen(bucket_key)
            module = Module(symbol, data_names, label_names,
                            logger=self.logger, context=self._context,
                            work_load_list=self._work_load_list,
                            fixed_param_names=self._fixed_param_names,
                            state_names=self._state_names)
            module.bind(data_shapes, label_shapes, self._curr_module.for_training,
                        self._curr_module.inputs_need_grad,
                        force_rebind=False, shared_module=self._buckets[self._default_bucket_key])
            self._buckets[bucket_key] = module

        self._curr_module = self._buckets[bucket_key]
        self._curr_bucket_key = bucket_key

    def init_optimizer(self, kvstore='local', optimizer='sgd',
                       optimizer_params=(('learning_rate', 0.01),),
                       force_init=False):
        """Install and initialize optimizers.

        Parameters
        ----------
        kvstore : str or KVStore
            Defaults to `'local'`.
        optimizer : str or Optimizer
            Defaults to `'sgd'`
        optimizer_params : dict
            Defaults to `(('learning_rate', 0.01),)`. The default value is not a dictionary,
            just to avoid pylint warning of dangerous default values.
        force_init : bool
            Defaults to ``False``, indicating whether we should force re-initializing the
            optimizer in the case an optimizer is already installed.
        """
        assert self.binded and self.params_initialized
        if self.optimizer_initialized and not force_init:
            self.logger.warning('optimizer already initialized, ignoring.')
            return

        self._curr_module.init_optimizer(kvstore, optimizer, optimizer_params,
                                         force_init=force_init)
        for mod in self._buckets.values():
            if mod is not self._curr_module:
                mod.borrow_optimizer(self._curr_module)

        self.optimizer_initialized = True

    def prepare(self, data_batch):
        '''Prepare a data batch for forward.

        Parameters
        ----------
        data_batch : DataBatch
        '''
        # perform bind if haven't done so
        assert self.binded and self.params_initialized
        bucket_key = data_batch.bucket_key
        original_bucket_key = self._curr_bucket_key
        data_shapes = data_batch.provide_data
        label_shapes = data_batch.provide_label
        self.switch_bucket(bucket_key, data_shapes, label_shapes)
        # switch back
        self.switch_bucket(original_bucket_key, None, None)

    def forward(self, data_batch, is_train=None):
        """Forward computation.

        Parameters
        ----------
        data_batch : DataBatch
        is_train : bool
            Defaults to ``None``, in which case `is_train` is take as ``self.for_training``.
        """
        assert self.binded and self.params_initialized
        self.switch_bucket(data_batch.bucket_key, data_batch.provide_data,
                           data_batch.provide_label)
        self._curr_module.forward(data_batch, is_train=is_train)

    def backward(self, out_grads=None):
        """Backward computation."""
        assert self.binded and self.params_initialized
        self._curr_module.backward(out_grads=out_grads)

    def update(self):
        """Update parameters according to installed optimizer and the gradient computed
        in the previous forward-backward cycle.
        """
        assert self.binded and self.params_initialized and self.optimizer_initialized
        self._params_dirty = True
        self._curr_module.update()

    def get_outputs(self, merge_multi_context=True):
        """Get outputs from a previous forward computation.

        Parameters
        ----------
        merge_multi_context : bool
            Defaults to ``True``. In the case when data-parallelism is used, the outputs
            will be collected from multiple devices. A ``True`` value indicate that we
            should merge the collected results so that they look like from a single
            executor.

        Returns
        -------
        If `merge_multi_context` is ``True``, it is like ``[out1, out2]``. Otherwise, it
        is like ``[[out1_dev1, out1_dev2], [out2_dev1, out2_dev2]]``. All the output
        elements are numpy arrays.
        """
        assert self.binded and self.params_initialized
        return self._curr_module.get_outputs(merge_multi_context=merge_multi_context)

    def get_input_grads(self, merge_multi_context=True):
        """Get the gradients with respect to the inputs of the module.

        Parameters
        ----------
        merge_multi_context : bool
            Defaults to ``True``. In the case when data-parallelism is used, the outputs
            will be collected from multiple devices. A ``True`` value indicate that we
            should merge the collected results so that they look like from a single
            executor.

        Returns
        -------
        If `merge_multi_context` is ``True``, it is like ``[grad1, grad2]``. Otherwise, it
        is like ``[[grad1_dev1, grad1_dev2], [grad2_dev1, grad2_dev2]]``. All the output
        elements are `NDArray`.
        """
        assert self.binded and self.params_initialized and self.inputs_need_grad
        return self._curr_module.get_input_grads(merge_multi_context=merge_multi_context)

    def update_metric(self, eval_metric, labels):
        """Evaluate and accumulate evaluation metric on outputs of the last forward computation.

        Parameters
        ----------
        eval_metric : EvalMetric
        labels : list of NDArray
            Typically ``data_batch.label``.
        """
        assert self.binded and self.params_initialized
        self._curr_module.update_metric(eval_metric, labels)

    @property
    def symbol(self):
        """The symbol of the current bucket being used."""
        assert self.binded
        return self._curr_module.symbol

    def install_monitor(self, mon):
        """ Install monitor on all executors """
        assert self.binded
        for mod in self._buckets.values():
            mod.install_monitor(mon)

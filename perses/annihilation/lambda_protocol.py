from __future__ import print_function
import numpy as np
import copy
import logging
import traceback
from openmmtools.alchemy import AlchemicalState

logging.basicConfig(level = logging.NOTSET)
_logger = logging.getLogger("lambda_protocol")
_logger.setLevel(logging.DEBUG)


class LambdaProtocol():
    """Protocols for perturbing each of the compent energy terms in alchemical
    free energy simulations.
    """

    default_functions = {'lambda_sterics_core':
                         lambda x: x,
                         'lambda_electrostatics_core':
                         lambda x: x,
                         'lambda_sterics_insert':
                         lambda x: 2.0 * x if x < 0.5 else 1.0,
                         'lambda_sterics_delete':
                         lambda x: 0.0 if x < 0.5 else 2.0 * (x - 0.5),
                         'lambda_electrostatics_insert':
                         lambda x: 0.0 if x < 0.5 else 2.0 * (x - 0.5),
                         'lambda_electrostatics_delete':
                         lambda x: 2.0 * x if x < 0.5 else 1.0,
                         'lambda_bonds':
                         lambda x: x,
                         'lambda_angles':
                         lambda x: x,
                         'lambda_torsions':
                         lambda x: x
                         }

    # lambda components for each component,
    # all run from 0 -> 1 following master lambda
    def __init__(self, type='default', protocol=None):
        """Instantiates lambda protocol to be used in a free energy calculation.
        Can either be user defined, using protocol, or using one of the preset
        options : default, namd or quarters.
        If both `protocol` and `type` are set, then `type` is ignored
        If `type` is not recognised, then it is set to default

        All protocols must be monotonic, from 0 to 1. Any energy term not defined
        in `protocol` will be set to the function in `default_functions`

        Parameters
        ----------
        type : str, default='default'
            one of the predefined lambda protocols ['default','namd','quarters']
        protocol : dict
            dictionary of lambda functions for each of the energy components,
            for both inserted and deleted and core atom types.

        Returns
        -------
        """
        self.type = type
        self.protocol = protocol
        if self.protocol is None:
            if self.type == 'default':
                self.functions = LambdaProtocol.default_functions
            elif self.type == 'namd':
                self.functions {'lambda_sterics_core':
                                lambda x: x,
                                'lambda_electrostatics_core':
                                lambda x: x,
                                'lambda_sterics_insert':
                                lambda x: (3. / 2.) * x if x < (2. / 3.) else 1.0,
                                'lambda_sterics_delete':
                                lambda x: 0.0 if x < (1. / 3.) else (x - (1. / 3.)) * (3. / 2.),
                                'lambda_electrostatics_insert':
                                lambda x: 0.0 if x < 0.5 else 2.0 * (x - 0.5),
                                'lambda_electrostatics_delete':
                                lambda x: 2.0 * x if x < 0.5 else 1.0,
                                'lambda_bonds':
                                lambda x: x,
                                'lambda_angles':
                                lambda x: x,
                                'lambda_torsions':
                                lambda x: x}
            elif self.type == 'quarters':
                self.functions =
            else:
                _logger.warning(f"""LambdaProtocol type : {self.type} not
                                  recognised. Allowed values are 'default',
                                  'namd' and 'quarters'. Setting LambdaProtocol
                                  functions to default. """)
                self.functions = LambdaProtocol.default_functions

        self._validate_functions()

    def _validate_functions(self,n=10):
        """Ensures that all the lambda functions adhere to the rules:
            - must begin at 0.
            - must finish at 1.
            - must be monotonically increasing

        Parameters
        ----------
        n : int, default 10
            number of grid points used to check monotonicity

        Returns
        -------

        """
        # the individual lambda functions that must be defined for
        required_functions = list(LambdaProtocol.default_functions.keys())

        for function in required_functions:
            if function in self.functions:
                continue
            else:
                _logger.warning('function {function} is missing from lambda_functions')
                _logger.warning('adding default {function} from LambdaProtocol.default_functions')
                self.functions[function] = LambdaProtocol.default_functions[function]
            # assert that the function starts and ends at 0 and 1 respectively
            assert (self.functions[function][0.] == 0.
                    ), 'lambda functions must start at 0'
            assert (self.functions[function][1.] == 1.
                    ), 'lambda functions must end at 1'

        # now validatate that it's monotonic
        global_lambda = np.linspace(0., 1., n)
        sub_lambda = [self.functions[function](l) for l in global_lambda]
        difference = np.diff(sub_lambda)
        assert (all(i >= 0. for i in difference)), 'lambda_schdeule must be monotonically increasing'
        return

    def get_functions(self):
        return self.functions


class RelativeAlchemicalState(AlchemicalState):
    """
    Relative AlchemicalState to handle all lambda parameters required for relative perturbations

    lambda = 1 refers to ON, i.e. fully interacting while
    lambda = 0 refers to OFF, i.e. non-interacting with the system

    all lambda functions will follow from 0 -> 1 following the master lambda

    lambda*core parameters perturb linearly
    lambda_sterics_insert and lambda_electrostatics_delete perturb in the first half of the protocol 0 -> 0.5
    lambda_sterics_delete and lambda_electrostatics_insert perturb in the second half of the protocol 0.5 -> 1

    Attributes
    ----------
    lambda_sterics_core
    lambda_electrostatics_core
    lambda_sterics_insert
    lambda_sterics_delete
    lambda_electrostatics_insert
    lambda_electrostatics_delete
    """

    class _LambdaParameter(AlchemicalState._LambdaParameter):
        pass

    lambda_sterics_core = _LambdaParameter('lambda_sterics_core')
    lambda_electrostatics_core = _LambdaParameter('lambda_electrostatics_core')
    lambda_sterics_insert = _LambdaParameter('lambda_sterics_insert')
    lambda_sterics_delete = _LambdaParameter('lambda_sterics_delete')
    lambda_electrostatics_insert = _LambdaParameter('lambda_electrostatics_insert')
    lambda_electrostatics_delete = _LambdaParameter('lambda_electrostatics_delete')

    def set_alchemical_parameters(self, global_lambda,
                                  lambda_protocol=LambdaProtocol()):
       """Set each lambda value according to the lambda_functions protocol.
       The undefined parameters (i.e. those being set to None) remain
       undefined.
       Parameters
       ----------
       lambda_value : float
           The new value for all defined parameters.
       """
       for parameter_name in lambda_protocol.functions:
           lambda_value = lambda_protocol.functions[parameter_name](global_lambda)
           setattr(self, parameter_name, lambda_value)

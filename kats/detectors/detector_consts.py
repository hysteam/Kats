# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast, List, Optional, Tuple, Union

import attr
import numpy as np
import numpy.typing as npt
import pandas as pd
from kats.consts import TimeSeriesData
from scipy.stats import norm, t, ttest_ind  # @manual
from statsmodels.stats import multitest

# from np.typing import ArrayLike
# pyre-fixme[24]: Generic type `np.ndarray` expects 2 type parameters.
ArrayLike = np.ndarray


# Single Spike object
@attr.s(auto_attribs=True)
class SingleSpike:
    time: datetime
    value: float
    n_sigma: float

    @property
    def time_str(self) -> str:
        return datetime.strftime(self.time, "%Y-%m-%d")


# Changepoint Interval object
@attr.s(auto_attribs=True)
class ChangePointInterval:
    start_time: datetime
    end_time: datetime
    previous_interval: Optional[ChangePointInterval] = attr.ib(default=None, init=False)
    _all_spikes: Union[
        Optional[List[SingleSpike]], Optional[List[List[SingleSpike]]]
    ] = attr.ib(default=None, init=False)
    spike_std_threshold: float = attr.ib(default=2.0, init=False)
    data_df: Optional[pd.DataFrame] = attr.ib(None, init=False)
    _ts_cols: List[str] = attr.ib(factory=lambda: ["value"], init=False)
    num_series: int = 1

    @property
    def data(self) -> Optional[ArrayLike]:
        df = self.data_df
        if df is None:
            return None
        elif self.num_series == 1:
            return df.value.values
        else:
            return df[self._ts_cols].values

    @data.setter
    def data(self, data: TimeSeriesData) -> None:
        if not data.is_univariate():
            self._ts_cols = list(data.value.columns)
            self.num_series = len(self._ts_cols)
        all_data_df = data.to_dataframe()
        all_data_df.columns = ["time"] + self._ts_cols
        all_data_df["time"] = pd.to_datetime(all_data_df["time"])
        all_data_df = all_data_df.loc[
            (all_data_df.time >= self.start_time) & (all_data_df.time < self.end_time)
        ]
        self.data_df = all_data_df

    def _detect_spikes(self) -> Union[List[SingleSpike], List[List[SingleSpike]]]:
        df = self.data_df
        if df is None:
            raise ValueError("data must be set before spike detection")

        if self.num_series == 1:
            df["z_score"] = (df.value - self.mean_val) / np.sqrt(self.variance_val)

            spike_df = df.query(f"z_score >={self.spike_std_threshold}")
            return [
                SingleSpike(
                    time=row["time"], value=row["value"], n_sigma=row["z_score"]
                )
                for counter, row in spike_df.iterrows()
            ]
        else:
            spikes = []
            for i, c in enumerate(self._ts_cols):
                mean_val, variance_val = self.mean_val, self.variance_val
                if isinstance(mean_val, float) or isinstance(variance_val, float):
                    raise ValueError(
                        f"num_series = {self.num_series} so mean_val and variance_val should have type ArrayLike."
                    )
                df[f"z_score_{c}"] = (df[c] - mean_val[i]) / np.sqrt(variance_val[i])

                spike_df = df.query(f"z_score_{c} >={self.spike_std_threshold}")

                if spike_df.shape[0] == 0:
                    continue
                else:
                    spikes.append(
                        [
                            SingleSpike(
                                time=row["time"],
                                value=row[c],
                                n_sigma=row[f"z_score_{c}"],
                            )
                            for counter, row in spike_df.iterrows()
                        ]
                    )
            return spikes

    def extend_data(self, data: TimeSeriesData) -> None:
        """
        extends the data.
        """
        new_data_df = data.to_dataframe()
        new_data_df.columns = ["time"] + self._ts_cols
        df = self.data_df
        if df is not None:
            new_data_df = pd.concat([df, new_data_df], copy=False)
        self.data_df = new_data_df.loc[
            (new_data_df.time >= self.start_time) & (new_data_df.time < self.end_time)
        ]

    @property
    def start_time_str(self) -> str:
        return datetime.strftime(self.start_time, "%Y-%m-%d")

    @property
    def end_time_str(self) -> str:
        return datetime.strftime(self.end_time, "%Y-%m-%d")

    @property
    def mean_val(self) -> Union[float, ArrayLike]:
        if self.num_series == 1:
            vals = self.data
            return 0.0 if vals is None else np.mean(vals)
        else:
            data_df = self.data_df
            if data_df is None:
                return np.zeros(self.num_series)
            return np.array([np.mean(data_df[c].values) for c in self._ts_cols])

    @property
    def variance_val(self) -> Union[float, ArrayLike]:
        if self.num_series == 1:
            vals = self.data
            # the t-test uses the sample standard deviation^2 instead of variance,
            return 0.0 if vals is None or len(vals) == 1 else np.var(vals, ddof=1)
        else:
            data_df = self.data_df
            if data_df is None or len(data_df) == 1:
                return np.zeros(self.num_series)
            # the t-test uses the sample standard deviation^2 instead of variance,
            return np.array([np.var(data_df[c].values, ddof=1) for c in self._ts_cols])

    def __len__(self) -> int:
        df = self.data_df
        return 0 if df is None else len(df)

    @property
    def spikes(self) -> Union[List[SingleSpike], List[List[SingleSpike]]]:
        spikes = self._all_spikes
        if spikes is None:
            spikes = self._detect_spikes()
            self._all_spikes = spikes
        return spikes


# Percentage Change Object
class PercentageChange:
    """
    PercentageChange is a class which is widely used in detector models. It calculates how much current TS changes
    compared to previous historical TS. It includes method to calculate t-scores, upper bound, lower bound, etc., for
    Statsig Detector model to detect significant change.

    Attributes:
        current: ChangePointInterval. The TS interval we'd like to detect.
        previous: ChangePointInterval. The historical TS interval, which we compare the current TS interval with.
        method: str, default value is "fdr_bh".
        skip_rescaling: bool, default value is False. For multi-variate TS, we need to rescale p-values so that alpha is still the threshold for rejection.
                    For Statsig detector, when a given TS (except historical part) is longer than max_split_ts_length,
                    we will transform this long univariate TS into a multi-variate TS and then use multistatsig detector instead.
                    In this case, we should skip rescaling p-values.
        use_corrected_scores: bool, default value is False, using original t-scores or correct t-scores.
        min_perc_change: float, minimum percentage change, for a non zero score. Score will be clipped to zero if the absolute value of the percentage chenge is less than this value
    """

    upper: Optional[Union[float, npt.NDArray]]
    lower: Optional[Union[float, npt.NDArray]]
    _t_score: Optional[Union[float, npt.NDArray]]
    _p_value: Optional[Union[float, npt.NDArray]]
    num_series: int

    def __init__(
        self,
        current: ChangePointInterval,
        previous: ChangePointInterval,
        method: str = "fdr_bh",
        skip_rescaling: bool = False,
        use_corrected_scores: bool = False,
        min_perc_change: float = 0.0,
    ) -> None:
        self.current = current
        self.previous = previous

        # pyre-fixme[4]: Attribute annotation cannot contain `Any`.
        self.upper = None
        # pyre-fixme[4]: Attribute annotation cannot contain `Any`.
        self.lower = None
        # pyre-fixme[4]: Attribute annotation cannot contain `Any`.
        self._t_score = None
        # pyre-fixme[4]: Attribute annotation cannot contain `Any`.
        self._p_value = None
        self.alpha = 0.05
        self.method = method
        self.num_series = self.current.num_series

        # If we'd like skip rescaling p-values for multivariate timeseires data
        self.skip_rescaling = skip_rescaling

        # 2 t scores strategies
        self.use_corrected_scores = use_corrected_scores
        self.min_perc_change = min_perc_change

    @property
    def ratio_estimate(self) -> Union[float, npt.NDArray]:
        # pyre-ignore[6]: Expected float for 1st positional only parameter to call float.__truediv__ but got Union[float, np.ndarray].
        return self.current.mean_val / self.previous.mean_val

    @property
    def perc_change(self) -> float:
        # pyre-fixme[7]: Expected `float` but got `Union[ndarray[Any, dtype[Any]],
        #  float]`.
        return (self.ratio_estimate - 1.0) * 100.0

    @property
    def perc_change_upper(self) -> float:
        if self.upper is None:
            self._delta_method()
        # pyre-fixme[7]: Expected `float` but got `Union[ndarray[Any, dtype[Any]],
        #  float]`.
        # pyre-fixme[24]: Generic type `np.ndarray` expects 2 type parameters.
        return (cast(Union[float, np.ndarray], self.upper) - 1) * 100.0

    @property
    def perc_change_lower(self) -> float:
        if self.lower is None:
            self._delta_method()
        # pyre-fixme[7]: Expected `float` but got `Union[ndarray[Any, dtype[Any]],
        #  float]`.
        # pyre-fixme[24]: Generic type `np.ndarray` expects 2 type parameters.
        return (cast(Union[float, np.ndarray], self.lower) - 1) * 100.0

    @property
    def direction(self) -> Union[str, ArrayLike]:
        if self.num_series > 1:
            return np.vectorize(lambda x: "up" if x > 0 else "down")(self.perc_change)
        elif self.perc_change > 0.0:
            return "up"
        else:
            return "down"

    @property
    def stat_sig(self) -> Union[bool, ArrayLike]:
        if self.upper is None:
            self._delta_method()
        if self.num_series > 1:
            return np.array(
                [
                    (
                        False
                        # pyre-fixme[24]: Generic type `np.ndarray` expects 2 type
                        #  parameters.
                        if cast(np.ndarray, self.upper)[i] > 1.0
                        # pyre-fixme[24]: Generic type `np.ndarray` expects 2 type
                        #  parameters.
                        and cast(np.ndarray, self.lower)[i] < 1
                        else True
                    )
                    for i in range(self.current.num_series)
                ]
            )
        # not stat sig e.g. [0.88, 1.55]
        return not (
            # pyre-fixme[24]: Generic type `np.ndarray` expects 2 type parameters.
            cast(Union[float, np.ndarray], self.upper) > 1.0
            # pyre-fixme[24]: Generic type `np.ndarray` expects 2 type parameters.
            and cast(Union[float, np.ndarray], self.lower) < 1.0
        )

    @property
    def score(self) -> float:
        if self._t_score is None:
            self._ttest()

        t_score = self._t_score

        if self.num_series == 1:
            if np.abs(self.perc_change) < self.min_perc_change:
                t_score = 0.0
        else:
            t_score = np.where(
                # pyre-fixme[6]: For 3rd argument expected `Union[_SupportsArray[dtyp...
                np.abs(self.perc_change) < self.min_perc_change,
                0,
                # pyre-fixme[6]: For 3rd argument expected `Union[_SupportsArray[dtyp...
                t_score,
            )

        return cast(float, t_score)

    @property
    def p_value(self) -> float:
        if self._p_value is None:
            self._ttest()
        return cast(float, self._p_value)

    @property
    def mean_previous(self) -> Union[float, npt.NDArray]:
        return self.previous.mean_val

    @property
    def mean_difference(self) -> Union[float, npt.NDArray]:
        # pyre-ignore[6]: Expected `float` for 1st param but got `Union[float,
        #  np.ndarray]`.
        _mean_diff = self.current.mean_val - self.previous.mean_val
        return _mean_diff

    @property
    def ci_upper(self) -> float:
        sp_mean = self._pooled_stddev()
        df = self._get_df()

        # the minus sign here is non intuitive.
        # this is because, for example, t.ppf(0.025, 30) ~ -1.96
        _ci_upper = self.previous.mean_val - t.ppf(self.alpha / 2, df) * sp_mean

        # pyre-fixme[7]: Expected `float` but got `Union[ndarray[Any, dtype[Any]],
        #  float]`.
        return _ci_upper

    @property
    def ci_lower(self) -> float:
        sp_mean = self._pooled_stddev()
        df = self._get_df()
        # the plus sign here is non-intuitive. See comment
        # above
        _ci_lower = self.previous.mean_val + t.ppf(self.alpha / 2, df) * sp_mean

        # pyre-fixme[7]: Expected `float` but got `Union[ndarray[Any, dtype[Any]],
        #  float]`.
        return _ci_lower

    def _get_df(self) -> float:
        """
        degree of freedom of t-test
        """
        n_1 = len(self.previous)
        n_2 = len(self.current)
        df = n_1 + n_2 - 2

        return df

    def _pooled_stddev(self) -> float:
        """
        This calculates the pooled standard deviation for t-test
        as defined in https://online.stat.psu.edu/stat500/lesson/7/7.3/7.3.1/7.3.1.1
        """

        s_1_sq = self.previous.variance_val
        s_2_sq = self.current.variance_val
        n_1 = len(self.previous)
        n_2 = len(self.current)

        # Require both populations to be nonempty, and their sum larger than 2, because the
        # t-test has (n_1 + n_2 - 2) degrees of freedom.
        if n_1 == 0 or n_2 == 0 or (n_1 == n_2 == 1):
            return 0.0

        # pyre-ignore[58]: * is not supported for operand types int and Union[float, np.ndarray].
        s_p = np.sqrt(((n_1 - 1) * s_1_sq + (n_2 - 1) * s_2_sq) / (n_1 + n_2 - 2))

        if not self.use_corrected_scores:
            return s_p

        # based on the definition of t-test, we should return s_p_mean
        s_p_mean = s_p * np.sqrt((1.0 / n_1) + (1.0 / n_2))
        return s_p_mean

    def _ttest_manual(self) -> Tuple[float, float]:
        """
        scipy's t-test gives nan when one of the arrays has a
        size of 1.
        To repro, run:
        >>> ttest_ind(np.array([1,2,3,4]), np.array([11]), equal_var=True, nan_policy='omit')
        This is implemented to fix this issue
        """
        sp_mean = self._pooled_stddev()
        df = self._get_df()

        # pyre-ignore[6]: Expected float for 1st positional only parameter to call float.__sub__ but got Union[float, np.ndarray].
        t_score = (self.current.mean_val - self.previous.mean_val) / sp_mean
        p_value = t.sf(np.abs(t_score), df) * 2  # sf = 1 - cdf

        return t_score, p_value

    def _ttest(self) -> None:
        if self.num_series > 1:
            self._ttest_multivariate()
            return

        n_1 = len(self.previous)
        n_2 = len(self.current)

        # if both control and test have one value, then using a t test does not make any sense
        # Return nan, which is the same as scipy's ttest_ind
        if n_1 == n_2 == 1:
            self._t_score = np.nan
            self._p_value = 0.0
            return

        # when sample size is 1, scipy's t test gives nan,
        # hence we separately handle this case
        # if n_1 == 1 or n_2 == 1:
        #     self._t_score, self._p_value = self._ttest_manual()
        # else:
        #     self._t_score, self._p_value = ttest_ind(
        #         current_data, prev_data, equal_var=True, nan_policy='omit'
        #     )

        # Always use ttest_manual because we changed the std to not include
        # np.sqrt((1. / n_1) + (1./ n_2))
        self._t_score, self._p_value = self._ttest_manual()

    def _ttest_multivariate(self) -> None:
        num_series = self.num_series
        p_value_start = np.zeros(num_series)
        t_value_start = np.zeros(num_series)

        n_1 = len(self.previous)
        n_2 = len(self.current)

        if n_1 == 1 and n_2 == 1:
            self._t_score = np.inf * np.ones(num_series)
            self._p_value = np.zeros(num_series)
            return
        elif n_1 == 1 or n_2 == 1:
            t_value_start, p_value_start = self._ttest_manual()
        else:
            current_data = self.current.data
            prev_data = self.previous.data
            if current_data is None or prev_data is None:
                raise ValueError("Interval data not set")
            for i in range(num_series):
                current_slice = current_data[:, i]
                prev_slice = prev_data[:, i]
                t_value_start[i], p_value_start[i] = ttest_ind(
                    current_slice, prev_slice, equal_var=True, nan_policy="omit"
                )

        # if un-scaled t_score and p_value are needed
        if self.skip_rescaling:
            self._p_value = p_value_start
            self._t_score = t_value_start
            return

        # The new p-values are the old p-values rescaled so that self.alpha is still the threshold for rejection
        _, self._p_value, _, _ = multitest.multipletests(
            p_value_start, alpha=self.alpha, method=self.method
        )
        self._t_score = np.zeros(num_series)
        # We are using a two-sided test here, so we take inverse_tcdf(self._p_value / 2) with df = len(self.current) + len(self.previous) - 2

        _t_score: npt.NDArray = self._t_score
        # pyre-fixme[24]: Generic type `np.ndarray` expects 2 type parameters.
        _p_value: npt.NDArray = cast(np.ndarray, self._p_value)
        for i in range(self.current.num_series):
            # pyre-fixme[16]: Item `float` of `ndarray[Any, dtype[Any]] | float` has
            #  no attribute `__getitem__`.
            if t_value_start[i] < 0:
                _t_score[i] = t.ppf(_p_value[i] / 2, self._get_df())
            else:
                _t_score[i] = t.ppf(1 - _p_value[i] / 2, self._get_df())
        self._t_score = _t_score

    def _calc_cov(self) -> float:
        """
        Calculates the covariance of x and y
        """
        current = self.current.data
        previous = self.previous.data
        if current is None or previous is None:
            return np.nan
        n_min = min(len(current), len(previous))
        if n_min == 0:
            return np.nan

        current = current[-n_min:]
        previous = previous[-n_min:]

        # for multivariate TS data
        if self.num_series > 1:
            # pyre-fixme[7]: Expected `float` but got `ndarray[Any, dtype[Any]]`.
            return np.asarray(
                [
                    np.cov(current[:, c], previous[:, c])[0, 1] / n_min
                    for c in range(self.num_series)
                ]
            )

        return np.cov(current, previous)[0, 1] / n_min

    def _delta_method(self) -> None:
        test_mean = self.current.mean_val
        control_mean = self.previous.mean_val
        test_var = self.current.variance_val
        control_var = self.previous.variance_val

        n_test = len(self.current)
        n_control = len(self.previous)

        cov_xy = self._calc_cov()

        sigma_sq_ratio = (
            # pyre-fixme[58]: `*` is not supported for operand types `int` and
            #  `Union[ndarray[Any, dtype[Any]], float]`.
            test_var / (n_test * (control_mean**2))
            # pyre-fixme[58]: `*` is not supported for operand types `int` and
            #  `Union[ndarray[Any, dtype[Any]], float]`.
            # pyre-fixme[58]: `/` is not supported for operand types `int` and
            #  `Union[ndarray[Any, dtype[Any]], float]`.
            - 2 * (test_mean * cov_xy) / (control_mean**3)
            # pyre-fixme[6]: For 1st argument expected `float` but got
            #  `Union[ndarray[Any, dtype[Any]], float]`.
            # pyre-fixme[58]: `*` is not supported for operand types `int` and
            #  `Union[ndarray[Any, dtype[Any]], float]`.
            + (control_var * (test_mean**2)) / (n_control * (control_mean**4))
        )
        # the signs appear flipped because norm.ppf(0.025) ~ -1.96
        self.lower = self.ratio_estimate + norm.ppf(self.alpha / 2) * np.sqrt(
            abs(sigma_sq_ratio)
        )
        self.upper = self.ratio_estimate - norm.ppf(self.alpha / 2) * np.sqrt(
            abs(sigma_sq_ratio)
        )


@dataclass
class ConfidenceBand:
    lower: TimeSeriesData
    upper: TimeSeriesData


class AnomalyResponse:
    key_mapping: List[str]
    num_series: int

    def __init__(
        self,
        scores: TimeSeriesData,
        confidence_band: Optional[ConfidenceBand],
        predicted_ts: Optional[TimeSeriesData],
        anomaly_magnitude_ts: TimeSeriesData,
        stat_sig_ts: Optional[TimeSeriesData],
    ) -> None:
        self.scores = scores
        self.confidence_band = confidence_band
        self.predicted_ts = predicted_ts
        self.anomaly_magnitude_ts = anomaly_magnitude_ts
        self.stat_sig_ts = stat_sig_ts

        self.key_mapping = []
        self.num_series = 1

        if not self.scores.is_univariate():
            self.num_series = len(scores.value.columns)
            self.key_mapping = list(scores.value.columns)

    def update(
        self,
        time: datetime,
        score: Union[float, ArrayLike],
        ci_upper: Union[float, ArrayLike],
        ci_lower: Union[float, ArrayLike],
        pred: Union[float, ArrayLike],
        anom_mag: Union[float, ArrayLike],
        stat_sig: Union[float, ArrayLike],
    ) -> None:
        """
        Add one more point and remove the last point
        """
        self.scores = self._update_ts_slice(self.scores, time, score)
        confidence_band = self.confidence_band
        if confidence_band is not None:
            self.confidence_band = ConfidenceBand(
                lower=self._update_ts_slice(confidence_band.lower, time, ci_lower),
                upper=self._update_ts_slice(confidence_band.upper, time, ci_upper),
            )

        predicted_ts = self.predicted_ts
        if predicted_ts is not None:
            self.predicted_ts = self._update_ts_slice(predicted_ts, time, pred)
        self.anomaly_magnitude_ts = self._update_ts_slice(
            self.anomaly_magnitude_ts, time, anom_mag
        )
        stat_sig_ts = self.stat_sig_ts
        if stat_sig_ts is not None:
            self.stat_sig_ts = self._update_ts_slice(stat_sig_ts, time, stat_sig)

    def _update_ts_slice(
        self, ts: TimeSeriesData, time: datetime, value: Union[float, ArrayLike]
    ) -> TimeSeriesData:
        time_df = pd.concat([ts.time.iloc[1:], pd.Series(time, copy=False)])
        time_df.reset_index(drop=True, inplace=True)
        if self.num_series == 1:
            value_df = pd.concat([ts.value.iloc[1:], pd.Series(value, copy=False)])
            value_df.reset_index(drop=True, inplace=True)
            # pyre-fixme[6]: For 1st argument expected `Union[None, DatetimeIndex,
            #  Series]` but got `DataFrame`.
            return TimeSeriesData(time=time_df, value=value_df)
        else:
            if isinstance(value, float):
                raise ValueError(
                    f"num_series = {self.num_series} so value should have type ArrayLike."
                )
            value_dict = {}
            for i, value_col in enumerate(self.key_mapping):
                value_dict[value_col] = pd.concat(
                    [ts.value[value_col].iloc[1:], pd.Series(value[i], copy=False)]
                )
                value_dict[value_col].reset_index(drop=True, inplace=True)
            return TimeSeriesData(
                pd.DataFrame(
                    {
                        **{"time": time_df},
                        **{
                            value_col: value_dict[value_col]
                            for value_col in self.key_mapping
                        },
                    },
                    copy=False,
                )
            )

    def inplace_update(
        self,
        time: datetime,
        score: Union[float, ArrayLike],
        ci_upper: Union[float, ArrayLike],
        ci_lower: Union[float, ArrayLike],
        pred: Union[float, ArrayLike],
        anom_mag: Union[float, ArrayLike],
        stat_sig: Union[float, ArrayLike],
    ) -> None:
        """
        Add one more point and remove the last point
        """
        self._inplace_update_ts(self.scores, time, score)
        cb = self.confidence_band
        if cb is not None:
            (self._inplace_update_ts(cb.lower, time, ci_lower),)
            self._inplace_update_ts(cb.upper, time, ci_upper)

        if self.predicted_ts is not None:
            self._inplace_update_ts(self.predicted_ts, time, pred)
        self._inplace_update_ts(self.anomaly_magnitude_ts, time, anom_mag)
        if self.stat_sig_ts is not None:
            self._inplace_update_ts(self.stat_sig_ts, time, stat_sig)

    def _inplace_update_ts(
        self,
        ts: Optional[TimeSeriesData],
        time: datetime,
        value: Union[float, ArrayLike],
    ) -> None:
        if ts is None:
            return
        if self.num_series == 1:
            ts.value.loc[ts.time == time] = value
        else:
            ts.value.loc[ts.time == time, :] = np.array(value, dtype=float)

    def get_last_n(self, N: int) -> AnomalyResponse:
        """
        returns the response for the last N days
        """
        cb = self.confidence_band
        pts = self.predicted_ts
        ssts = self.stat_sig_ts

        return AnomalyResponse(
            scores=self.scores[-N:],
            confidence_band=(
                None
                if cb is None
                else ConfidenceBand(
                    upper=cb.upper[-N:],
                    lower=cb.lower[-N:],
                )
            ),
            predicted_ts=None if pts is None else pts[-N:],
            anomaly_magnitude_ts=self.anomaly_magnitude_ts[-N:],
            stat_sig_ts=None if ssts is None else ssts[-N:],
        )

    def extend(self, other: "AnomalyResponse", validate: bool = True) -> None:
        """
        Extends :class:`AnomalyResponse` with another :class:`AnomalyResponse`
        object.

        Args:
          other: The other :class:`AnomalyResponse` object.
          validate (optional): A boolean representing if the contained
            :class:`TimeSeriesData` objects should be validated after
            concatenation (default True).

        Raises:
          ValueError: Validation failed, or some of the components of this
            :class:`AnomalyResponse` are None while `other`'s are not (or vice
            versa).
        """
        if not isinstance(other, AnomalyResponse):
            raise TypeError("extend must take another AnomalyResponse object")
        component_mismatch_error_msg = (
            "The {} in one of the AnomalyResponse objects is None while the "
            "other is not. Either both should be None or neither."
        )
        if (self.confidence_band is None) ^ (other.confidence_band is None):
            raise ValueError(component_mismatch_error_msg.format("confidence_band"))
        if (self.predicted_ts is None) ^ (other.predicted_ts is None):
            raise ValueError(component_mismatch_error_msg.format("predicted_ts"))
        if (self.stat_sig_ts is None) ^ (other.stat_sig_ts is None):
            raise ValueError(component_mismatch_error_msg.format("stat_sig_ts"))

        self.scores.extend(other.scores, validate=validate)
        if self.confidence_band is not None:
            cb = self.confidence_band
            other_cb = cast(ConfidenceBand, other.confidence_band)
            cb.upper.extend(other_cb.upper, validate=validate)
            cb.lower.extend(other_cb.lower, validate=validate)
        if self.predicted_ts is not None:
            self.predicted_ts.extend(other.predicted_ts, validate=validate)
        self.anomaly_magnitude_ts.extend(other.anomaly_magnitude_ts, validate=validate)
        if self.stat_sig_ts is not None:
            self.stat_sig_ts.extend(other.stat_sig_ts, validate=validate)

    def __str__(self) -> str:
        cb = self.confidence_band
        upper = None if cb is None else cb.upper.value.values
        lower = None if cb is None else cb.lower.value.values
        predicted = (
            None if self.predicted_ts is None else self.predicted_ts.value.values
        )
        statsig = None if self.stat_sig_ts is None else self.stat_sig_ts.value.values
        str_ret = f"""
        Time: {self.scores.time.values},
        Scores: {self.scores.value.values},
        Upper Confidence Bound: {upper},
        Lower Confidence Bound: {lower},
        Predicted Time Series: {predicted},
        stat_sig:{statsig}
        """

        return str_ret
